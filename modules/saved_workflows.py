"""
The workflows saved in ComfyUI, made into what the pipeline runs: an API-format
workflow and the '.map.json' next to it.

ComfyUI saves what its editor shows, and only its web page turns that into what
its API takes. So this does the same for the parts that exist only in the editor:
Set/Get nodes, reroutes, primitives, and bypassed or muted nodes. Which inputs to
fill is worked out from the graph. Anything that won't run says why, rather than
failing an hour into a queue.
"""
import json
import os
import re
from urllib.parse import quote

import requests

# Only in the editor: nothing of these reaches ComfyUI's API.
NOTES = ("Note", "MarkdownNote")
PASS_THROUGH = ("SetNode", "GetNode", "Reroute")
BYPASSED, MUTED = 4, 2
SEED_CONTROLS = ("fixed", "increment", "decrement", "randomize")
# Latent video comes in 4n+1 frames.
FRAME_STEP = 4
PROMPT_INPUTS = ("text", "prompt", "value")
# SaveVideo always writes to ComfyUI's output folder, which the pipeline stays out of.
COMBINE = "VHS_VideoCombine"
COMBINE_DEFAULTS = {"loop_count": 0, "filename_prefix": "pipeline", "format": "video/h264-mp4", "pix_fmt": "yuv420p",
                    "crf": 19, "save_metadata": False, "trim_to_audio": False, "pingpong": False, "save_output": False}


class Unusable(Exception):
    """Why the pipeline can't run this workflow, in words for the panel."""


def _input_spec(spec):
    inputs = spec.get("input", {})
    return {**inputs.get("required", {}), **inputs.get("optional", {})}


def _options(config):
    """A dropdown's choices, or None if the input isn't one, or takes files the pipeline fills in."""
    extra = config[1] if len(config) > 1 and isinstance(config[1], dict) else {}
    if any(key in extra for key in ("image_upload", "audio_upload", "video_upload")):
        return None
    return config[0] if isinstance(config[0], list) else extra.get("options") if config[0] == "COMBO" else None


class _Graph:
    def __init__(self, ui, info):
        self.info = info
        self.links = {}
        for link in ui.get("links", []):
            if isinstance(link, dict):
                link = [link["id"], link["origin_id"], link["origin_slot"], link["target_id"], link["target_slot"], link["type"]]
            self.links[link[0]] = link
        self.nodes = {node["id"]: node for node in ui["nodes"]}
        self.setters = {(node.get("widgets_values") or [None])[0]: node for node in ui["nodes"] if node["type"] == "SetNode"}

    def skipped(self, node):
        # The form offers every image input, so a bypassed one still counts: left empty, it comes out again.
        return node.get("mode") in (BYPASSED, MUTED) and node["type"] != "LoadImage"

    def source(self, link_id, seen=()):
        """What really feeds this link: [node id, output slot], {'value': a primitive's}, or None."""
        link = self.links.get(link_id)
        origin = self.nodes.get(link[1]) if link else None
        if not origin or origin["id"] in seen:
            return None
        seen = (*seen, origin["id"])
        kind = origin["type"]
        if kind == "PrimitiveNode":
            return {"value": origin["widgets_values"][0]}
        if kind == "GetNode":
            setter = self.setters.get((origin.get("widgets_values") or [None])[0])
            inputs = setter.get("inputs", []) if setter else []
            return self.source(inputs[0].get("link"), seen) if inputs else None
        if kind in ("SetNode", "Reroute"):
            inputs = origin.get("inputs", [])
            return self.source(inputs[0].get("link"), seen) if inputs else None
        if self.skipped(origin):
            return self._through_bypass(origin, link[2], seen) if origin["mode"] == BYPASSED else None
        return [str(origin["id"]), link[2]]

    def _through_bypass(self, node, slot, seen):
        """A bypassed node hands on the input of its output's type: the one in the same position if it can."""
        outputs, inputs = node.get("outputs", []), node.get("inputs", [])
        wanted = outputs[slot].get("type") if slot < len(outputs) else None
        matching = [i for i, entry in enumerate(inputs) if entry.get("type") == wanted and entry.get("link") is not None]
        if not matching:
            return None
        return self.source(inputs[slot if slot in matching else matching[0]].get("link"), seen)

    def inputs_of(self, node):
        inputs = {}
        values = node.get("widgets_values")
        if isinstance(values, dict):
            inputs.update({name: value for name, value in values.items() if name != "videopreview"})
        else:
            values = list(values or [])
            widgets = [entry["widget"]["name"] for entry in node.get("inputs", []) if "widget" in entry]
            position = 0
            for name in widgets:
                if position >= len(values):
                    break
                inputs[name] = values[position]
                position += 1
                # The dropdown saved after a seed, which only the editor reads.
                if position < len(values) and values[position] in SEED_CONTROLS and isinstance(inputs[name], int):
                    position += 1
            # Widgets a node's own script added, such as Power Lora Loader's list.
            loras = [value for value in values[position:] if isinstance(value, dict) and "lora" in value]
            inputs.update({f"lora_{number}": lora for number, lora in enumerate(loras, 1)})
        for entry in node.get("inputs", []):
            if entry.get("link") is not None:
                source = self.source(entry["link"])
                if isinstance(source, dict):
                    inputs[entry["name"]] = source["value"]
                elif source:
                    inputs[entry["name"]] = source
        return inputs


def _node(class_type, inputs, title):
    return {"inputs": inputs, "class_type": class_type, "_meta": {"title": title}}


def _keep_out_of_output_folder(api, info):
    """Swaps SaveVideo + CreateVideo for the combine node, which can save to ComfyUI's temp folder instead."""
    for node in api.values():
        if node["class_type"] != "SaveVideo":
            continue
        made_by = node["inputs"].get("video")
        maker = api.get(made_by[0]) if isinstance(made_by, list) else None
        if not maker or maker["class_type"] != "CreateVideo":
            raise Unusable("Its SaveVideo isn't fed by CreateVideo, so its clips can't stay out of ComfyUI's output folder.")
        if COMBINE not in info:
            raise Unusable("It uses SaveVideo. Install ComfyUI-VideoHelperSuite to keep its clips out of ComfyUI's output folder.")
        inputs = {**COMBINE_DEFAULTS, "images": maker["inputs"]["images"], "frame_rate": maker["inputs"]["fps"]}
        if "audio" in maker["inputs"]:
            inputs["audio"] = maker["inputs"]["audio"]
        node.update(_node(COMBINE, inputs, "Video Combine"))


def _upstream(api, roots):
    """Only what the video needs: ComfyUI wouldn't run the rest, and a preview would save files of its own."""
    kept, waiting = set(), list(roots)
    while waiting:
        node_id = waiting.pop()
        if node_id in kept or node_id not in api:
            continue
        kept.add(node_id)
        waiting += [value[0] for value in api[node_id]["inputs"].values() if isinstance(value, list) and len(value) == 2]
    return {node_id: api[node_id] for node_id in api if node_id in kept}


def _check_installed(api, info):
    for node in api.values():
        spec = _input_spec(info[node["class_type"]])
        for name, config in spec.items():
            value = node["inputs"].get(name)
            options = _options(config)
            if options and value is not None and not isinstance(value, list) and value not in options:
                raise Unusable(f"Needs {value}, which isn't installed.")
        for name, config in info[node["class_type"]].get("input", {}).get("required", {}).items():
            # A growing list of inputs is saved as name.a, name.b, and can be empty.
            if name not in node["inputs"] and config[0] != "COMFY_AUTOGROW_V3":
                raise Unusable(f"{node['_meta']['title']} has nothing set for {name}.")


def to_api(ui, info):
    """The saved workflow as ComfyUI's API takes it. Raises Unusable."""
    if ui.get("definitions", {}).get("subgraphs"):
        raise Unusable("It uses subgraphs. Unpack them in ComfyUI and save it again.")
    graph = _Graph(ui, info)
    api = {}
    for node in ui["nodes"]:
        kind = node["type"]
        if kind in NOTES or kind in PASS_THROUGH or kind == "PrimitiveNode" or graph.skipped(node):
            continue
        if kind not in info:
            raise Unusable(f"Needs the node {kind}, which isn't installed.")
        title = node.get("title") or info[kind].get("display_name") or kind
        api[str(node["id"])] = _node(kind, graph.inputs_of(node), title)

    _keep_out_of_output_folder(api, info)
    saves = [node_id for node_id, node in api.items() if "save_output" in node["inputs"]]
    if not saves:
        raise Unusable("It doesn't make a video the pipeline can pick up.")
    api = _upstream(api, saves)
    _check_installed(api, info)
    return api


def _titled(api, pattern, kinds):
    """The node with this title holding one value of these kinds, as [node id, input]."""
    for node_id, node in api.items():
        if re.search(pattern, node["_meta"]["title"], re.I):
            values = [name for name, value in node["inputs"].items() if type(value) in kinds]
            if len(values) == 1:
                return [node_id, values[0]]
    return None


def _prompt_input(api):
    """Walks up from what the sampler treats as positive, or a node that takes a prompt itself, to the text it started as."""
    waiting = [value[0] for node in api.values() for name, value in node["inputs"].items()
               if name == "positive" and isinstance(value, list)]
    if not waiting:
        for node_id, node in api.items():
            prompt = node["inputs"].get("prompt")
            if isinstance(prompt, str):
                return [node_id, "prompt"]
            if isinstance(prompt, list):
                waiting.append(prompt[0])
    seen = set()
    while waiting:
        node_id = waiting.pop(0)
        if node_id in seen or node_id not in api:
            continue
        seen.add(node_id)
        inputs = api[node_id]["inputs"]
        for name in PROMPT_INPUTS:
            if isinstance(inputs.get(name), str):
                return [node_id, name]
        waiting += [value[0] for name, value in inputs.items() if isinstance(value, list) and name != "negative"]
    return None


def _one_input(api, class_type, name, what):
    found = [node_id for node_id, node in api.items() if node["class_type"] == class_type]
    if len(found) > 1:
        raise Unusable(f"It takes {len(found)} {what} inputs, and the pipeline only has one to give.")
    return [found[0], name] if found else None


def _image_fields(api, info):
    """
    image, image_2... for each Load Image, in the order the node using them numbers
    them. image_link is where one is plugged in, if the workflow also runs without it.
    """
    found = []
    for node_id, node in api.items():
        if node["class_type"] != "LoadImage":
            continue
        users = [(user_id, name) for user_id, user in api.items() for name, value in user["inputs"].items()
                 if isinstance(value, list) and value[0] == node_id]
        found.append((users[0][1] if users else "", int(node_id), users))
    fields = {}
    for number, (_, node_id, users) in enumerate(sorted(found, key=lambda entry: entry[:2]), 1):
        slot = "image" if number == 1 else f"image_{number}"
        fields[slot] = [str(node_id), "image"]
        if len(users) == 1:
            user_id, name = users[0]
            if name.split(".")[0] in info[api[user_id]["class_type"]].get("input", {}).get("optional", {}):
                fields[f"{slot}_link"] = [user_id, name]
    return fields


def image_slots(fields):
    """The image inputs a map fills, in order."""
    return [field for field in fields if re.fullmatch(r"image(_\d+)?", field)]


def takes(fields):
    """How many images a workflow can be given. One it can't run without needs a switch to text, or an image."""
    slots = image_slots(fields)
    needed = 0 if "text_to_video" in fields else sum(f"{slot}_link" not in fields for slot in slots)
    return {"min_images": needed, "max_images": len(slots)}


def find_fields(api, info):
    """Which inputs the pipeline fills: {field: [node id, input]}. Raises Unusable."""
    fields = {"prompt": _prompt_input(api), **_image_fields(api, info)}
    if not fields["prompt"]:
        raise Unusable("Couldn't find its prompt: nothing feeds the sampler's positive input, or a prompt input.")
    audio = _one_input(api, "LoadAudio", "audio", "audio")
    if audio:
        fields["audio"] = audio

    switch = _titled(api, r"text.?to.?video", (bool,))
    if switch:
        fields["text_to_video"] = switch
    seconds = _titled(api, r"\b(seconds?|duration)\b", (int, float))
    width, height = _titled(api, r"^width$", (int,)), _titled(api, r"^height$", (int,))
    size_node = next((node_id for node_id, node in api.items()
                      if all(k in node["inputs"] for k in ("width", "height", "length"))), None)
    if seconds:
        fields["seconds"] = seconds
    elif size_node:
        rate_node = next((node_id for node_id, node in api.items() if isinstance(node["inputs"].get("frame_rate"), (int, float))), None)
        if not rate_node:
            raise Unusable("Couldn't find its frame rate, so it can't be told how long a clip is.")
        fields["frames"], fields["fps"] = [size_node, "length"], [rate_node, "frame_rate"]
    else:
        raise Unusable("Couldn't find how to set a clip's length: no input titled in seconds, or length on its latent.")
    if width and height:
        fields["width"], fields["height"] = width, height
    elif size_node:
        fields["width"], fields["height"] = [size_node, "width"], [size_node, "height"]
    return fields


def frames_for(seconds, fps):
    return round(seconds * fps / FRAME_STEP) * FRAME_STEP + 1


# --- Fetching them from ComfyUI, and keeping what the pipeline runs ---

def _write_json(path, data):
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(temp, path)


def _label(name):
    return name[:-5] if name.lower().endswith(".json") else name


def _file_stem(name):
    return re.sub(r"[^\w.-]+", "_", _label(name))


def _prepare(name, ui, info, folder):
    """Writes what the pipeline runs. Returns its path, and how many images it takes."""
    api = to_api(ui, info)
    fields = find_fields(api, info)
    stem = os.path.join(folder, _file_stem(name))
    _write_json(stem + ".json", api)
    _write_json(stem + ".map.json", {"_saved_as": name, **{field: f"{node_id}.{input_name}" for field, (node_id, input_name) in fields.items()}})
    return stem + ".json", takes(fields)


def _from_comfyui(url, folder):
    names = requests.get(f"{url}/api/userdata", params={"dir": "workflows", "recurse": "true"}, timeout=5).json()
    # Takes a few seconds: ComfyUI asks every node it has.
    info = requests.get(f"{url}/object_info", timeout=60).json()
    os.makedirs(folder, exist_ok=True)
    found = []
    for name in sorted(n for n in names if n.lower().endswith(".json")):
        entry = {"name": name, "label": _label(name)}
        try:
            response = requests.get(f"{url}/api/userdata/{quote('workflows/' + name, safe='')}", timeout=10)
            response.raise_for_status()
            ui = response.json()
            if "nodes" not in ui:
                raise Unusable("It's in API format, which has no way to tell which inputs to fill. Save it from the editor.")
            entry["path"], entry["takes"] = _prepare(name, ui, info, folder)
        except Unusable as e:
            entry["problem"] = str(e)
        except (KeyError, IndexError, TypeError, AttributeError, ValueError, requests.RequestException) as e:
            entry["problem"] = f"Couldn't read it ({e})."
        found.append(entry)
    _forget_deleted(folder, found)
    return found


def _forget_deleted(folder, found):
    keep = {os.path.basename(entry["path"]) for entry in found if "path" in entry}
    for file in os.listdir(folder):
        if file.endswith(".map.json") and file[:-len(".map.json")] + ".json" not in keep:
            for stale in (file, file[:-len(".map.json")] + ".json"):
                if os.path.exists(os.path.join(folder, stale)):
                    os.remove(os.path.join(folder, stale))


def _from_earlier(folder):
    """With ComfyUI off: the ones it had last time. A run starts it, so they can still be chosen."""
    found = []
    if not os.path.isdir(folder):
        return found
    for file in sorted(os.listdir(folder)):
        if file.endswith(".map.json"):
            path = os.path.join(folder, file[:-len(".map.json")] + ".json")
            try:
                with open(os.path.join(folder, file), encoding="utf-8") as f:
                    fields = json.load(f)
                name = fields["_saved_as"]
            except (OSError, ValueError, KeyError):
                continue
            if os.path.exists(path):
                found.append({"name": name, "label": _label(name), "path": path, "takes": takes(fields)})
    return found


def list_saved(url, folder):
    """Each saved workflow: {name, label, and path and takes to run it, or problem saying why it can't}."""
    try:
        return _from_comfyui(url, folder)
    except (OSError, ValueError, requests.RequestException):
        return _from_earlier(folder)
