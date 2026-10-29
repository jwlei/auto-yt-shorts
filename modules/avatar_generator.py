import copy
import json
import math
import os
import random
import time

import ffmpeg
import requests
from dotenv import load_dotenv

# Where avatar scenes come from, chosen with AVATAR_SOURCE in .env:
# 'static' loops assets/avatar/avatars.mp4; 'comfyui' renders a fresh clip per scene.
AVATAR_SOURCES = ("static", "comfyui")
DEFAULT_AVATAR_SOURCE = "static"

DEFAULT_COMFYUI_URL = "http://127.0.0.1:8188"
# Lip-sync variant: the scene's narration drives the presenter's mouth.
# comfyui/avatar_workflow.json is the same workflow without it (silent reactions).
DEFAULT_WORKFLOW = os.path.join("comfyui", "avatar_lipsync.json")
DEFAULT_SIZE = "736x1280"  # Portrait, so the composer's 9:16 crop keeps the whole frame

# Used only for text-to-video, when there is no reference image to carry the look.
DEFAULT_PERSONA = (
    "A woman in her thirties with dark hair in a messy bun and round glasses, wearing a dark "
    "green cardigan over a grey t-shirt, sits at a desk in a warm home office with bookshelves "
    "and plants behind her. Medium shot, soft natural window light."
)

COMFYUI_CLIENT_ID = "aishorts"
VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".mkv", ".gif")
SEED_INPUTS = ("seed", "noise_seed")
POLL_INTERVAL = 5


# Which scenes show the avatar:
#   auto - opens on it, then 3-5 avatar scenes in total, picked after the stock
#          footage is in: scenes whose footage fits the narration badly go to the avatar
#   3    - opens on it + two random middle scenes
#   2    - two random middle scenes only
#   0    - none
AVATAR_LAYOUTS = ("auto", "3", "2", "0")
DEFAULT_AVATAR_LAYOUT = "auto"
AUTO_MIN, AUTO_MAX = 3, 5
# Footage rated at or below this (0-10) counts as a poor fit and pulls in the avatar.
POOR_FIT = 4


def default_avatar_layout():
    """Returns AVATAR_CLIPS from .env, or raises if it is not a known layout."""
    value = (os.getenv("AVATAR_CLIPS") or DEFAULT_AVATAR_LAYOUT).strip().lower()
    if value not in AVATAR_LAYOUTS:
        raise RuntimeError(f"AVATAR_CLIPS='{value}' is not supported. Use one of: {', '.join(AVATAR_LAYOUTS)}.")
    return value


def plan_avatar_scenes(script_data, fits, eligible):
    """
    Picks the avatar scenes for the 'auto' layout. Returns sorted scene indices.

    - The hook (scene 1) always opens on the avatar.
    - The total starts at a random 3 or 4; every scene with poor footage (fit <=
      POOR_FIT, or none found) beyond that raises it, up to 5.
    - Poorest-fitting scenes are taken first; the rest are drawn at random,
      weighted towards weaker footage, so videos don't all look the same.

    fits: {index: 0-10} from ContentBrain.rate_footage_fit.
    eligible: indices the avatar can cover (lip-synced clips can't run past the clip cap).
    """
    eligible = [i for i in eligible if i < len(script_data)]
    if not eligible:
        return []

    poor = sorted((i for i in eligible if i != 0 and fits.get(i, 5) <= POOR_FIT), key=lambda i: fits.get(i, 5))
    base = random.randint(AUTO_MIN, AUTO_MAX - 1)
    count = min(AUTO_MAX, max(base, 1 + len(poor)), len(eligible))

    chosen = [0] if 0 in eligible else []
    for i in poor:
        if len(chosen) >= count:
            break
        chosen.append(i)

    rest = [i for i in eligible if i not in chosen]
    while len(chosen) < count and rest:
        pick = random.choices(rest, weights=[11 - fits.get(i, 5) for i in rest])[0]
        chosen.append(pick)
        rest.remove(pick)

    chosen.sort()
    weak = sum(1 for i in chosen if fits.get(i, 5) <= POOR_FIT)
    print(f"🎲 Avatar plan: {len(chosen)} scenes {[script_data[i]['id'] for i in chosen]} "
          f"({weak} replace weak footage, base {base})")
    return chosen


def get_avatar_source():
    """Returns the configured avatar source, or raises if AVATAR_SOURCE is unknown."""
    source = (os.getenv("AVATAR_SOURCE") or DEFAULT_AVATAR_SOURCE).strip().lower()
    if source not in AVATAR_SOURCES:
        raise RuntimeError(f"AVATAR_SOURCE='{source}' is not supported. Use one of: {', '.join(AVATAR_SOURCES)}.")
    return source


class AvatarGenerator:
    """
    Renders presenter clips with a ComfyUI workflow exported in API format.

    The workflow sits next to a '.map.json' that says which node inputs to fill,
    as "<node id>.<input name>":
      prompt (required), image, audio, text_to_video, seconds, width, height.
    Any 'seed' / 'noise_seed' input is randomised on every run. Mapping 'audio'
    turns on lip-sync: each clip is generated to match its scene's narration.
    """

    def __init__(self):
        load_dotenv()
        self.url = (os.getenv("COMFYUI_URL") or DEFAULT_COMFYUI_URL).rstrip("/")
        self.workflow_path = os.getenv("COMFYUI_WORKFLOW") or os.path.join(os.getcwd(), DEFAULT_WORKFLOW)
        self.timeout = int(os.getenv("COMFYUI_TIMEOUT") or 1800)
        self.max_seconds = int(os.getenv("AVATAR_MAX_SECONDS") or 10)
        self.persona = os.getenv("AVATAR_PERSONA") or DEFAULT_PERSONA
        self.reference_image = os.getenv("AVATAR_REFERENCE_IMAGE") or None
        # Horizontal centre of the portrait crop taken from a wider reference image (0 = left, 1 = right).
        self.focus_x = min(1.0, max(0.0, float(os.getenv("AVATAR_FOCUS_X") or 0.5)))

        size = (os.getenv("AVATAR_SIZE") or DEFAULT_SIZE).lower().split("x")
        try:
            self.width, self.height = int(size[0]), int(size[1])
        except (ValueError, IndexError):
            raise RuntimeError(f"AVATAR_SIZE must look like 736x1280, got '{os.getenv('AVATAR_SIZE')}'.")

        self.workflow, self.fields = self._load_workflow()
        self.lipsync = "audio" in self.fields

        if self.reference_image and not os.path.exists(self.reference_image):
            raise RuntimeError(f"AVATAR_REFERENCE_IMAGE not found: {self.reference_image}")
        if self.reference_image and "image" not in self.fields:
            print("⚠️ AVATAR_REFERENCE_IMAGE is set but the workflow map has no 'image' field — ignoring it.")
            self.reference_image = None

        self.clips_dir = os.path.join(os.getcwd(), "assets", "video_clips")
        os.makedirs(self.clips_dir, exist_ok=True)
        # Fixed, so the web UI can listen on ComfyUI's websocket for this run's
        # progress (ComfyUI only sends progress to the client that queued the job).
        self.client_id = os.getenv("COMFYUI_CLIENT_ID") or COMFYUI_CLIENT_ID

    def _load_workflow(self):
        map_path = os.path.splitext(self.workflow_path)[0] + ".map.json"
        for path in (self.workflow_path, map_path):
            if not os.path.exists(path):
                raise RuntimeError(f"ComfyUI workflow file missing: {path}")

        with open(self.workflow_path, "r", encoding="utf-8") as f:
            workflow = json.load(f)
        with open(map_path, "r", encoding="utf-8") as f:
            raw_map = json.load(f)

        if "nodes" in workflow:
            raise RuntimeError(f"{self.workflow_path} is a UI workflow. Export it with 'Export (API)' instead.")

        fields = {}
        for field, path in raw_map.items():
            if field.startswith("_"):
                continue  # Comments
            node_id, _, input_name = str(path).partition(".")
            if input_name not in workflow.get(node_id, {}).get("inputs", {}):
                raise RuntimeError(f"{map_path}: '{field}' points at '{path}', which is not in the workflow.")
            fields[field] = (node_id, input_name)

        if "prompt" not in fields:
            raise RuntimeError(f"{map_path} must map 'prompt' to the positive prompt input.")
        return workflow, fields

    def can_render(self, scene):
        """A lip-synced clip must cover the whole line; looping it would desync the mouth."""
        return not self.lipsync or math.ceil(scene.get('duration', 0) + 0.5) <= self.max_seconds

    def is_reachable(self):
        try:
            return requests.get(f"{self.url}/system_stats", timeout=5).status_code == 200
        except requests.RequestException:
            return False

    # --- Pipeline entry point ---

    def generate(self, script_data, indices, brain):
        """
        Renders one clip for each scene index. Returns {index: clip path} for the
        clips that succeeded; the caller decides what to show for the rest.

        With AVATAR_REFERENCE_IMAGE every clip is image-to-video from it. Without
        one, the first clip is text-to-video from AVATAR_PERSONA and a frame of it
        becomes the reference for the rest, so the same person appears throughout.
        """
        print(f"🧑 Generating {len(indices)} avatar clip(s) with ComfyUI ({self.url})"
              f"{', lip-synced' if self.lipsync else ''}...")
        clips = {}
        reference = None

        try:
            if self.reference_image:
                reference = self._upload(self._portrait_crop(self.reference_image), "aishorts_avatar_ref.png")
        except Exception as e:
            print(f"   ❌ Could not upload the reference image: {e}")
            return clips

        for number, i in enumerate(indices, 1):
            scene = script_data[i]
            needed = math.ceil(scene.get('duration', self.max_seconds) + 0.5)
            if self.lipsync and needed > self.max_seconds:
                # Looping a lip-synced clip would put the mouth out of step with the voice.
                print(f"   ⚠️ Scene {scene['id']} narration is longer than {self.max_seconds}s — using stock footage.")
                continue
            seconds = min(self.max_seconds, max(3, needed))
            action = brain.write_avatar_prompt(scene, seconds, speaking=self.lipsync)
            # The reference image already shows the persona; restating it fights the image.
            prompt = action if reference else f"{self.persona}\n\n{action}"

            mode = "image-to-video" if reference else "text-to-video"
            print(f"   🎬 Clip {number}/{len(indices)} · scene {scene['id']}: {seconds}s {mode}")
            try:
                audio = self._upload(self._narration(scene, seconds), "aishorts_avatar_voice.wav") if self.lipsync else None
                clip = self._render(prompt, reference, audio, seconds, f"avatar_scene_{scene['id']}")
            except Exception as e:
                print(f"   ❌ Scene {scene['id']} avatar failed: {e}")
                clip = None
            if not clip:
                continue

            clips[i] = clip
            if reference is None and "image" in self.fields:
                try:
                    reference = self._reference_from(clip)
                except Exception as e:
                    print(f"   ⚠️ Could not reuse scene {scene['id']} as the persona reference: {e}")

        return clips

    # --- ComfyUI API ---

    def _set(self, workflow, field, value):
        node_id, input_name = self.fields[field]
        workflow[node_id]["inputs"][input_name] = value

    def _render(self, prompt, reference, audio, seconds, name):
        workflow = copy.deepcopy(self.workflow)
        self._set(workflow, "prompt", prompt)
        if "seconds" in self.fields:
            self._set(workflow, "seconds", seconds)
        if "width" in self.fields and "height" in self.fields:
            self._set(workflow, "width", self.width)
            self._set(workflow, "height", self.height)
        if "text_to_video" in self.fields:
            self._set(workflow, "text_to_video", reference is None)
        if reference:
            self._set(workflow, "image", reference)
        if audio:
            self._set(workflow, "audio", audio)

        # Fresh seeds every run: ComfyUI would otherwise return its cached result.
        for node in workflow.values():
            for key in SEED_INPUTS:
                if isinstance(node.get("inputs", {}).get(key), int):
                    node["inputs"][key] = random.randint(0, 2**32 - 1)

        response = requests.post(f"{self.url}/prompt", json={"prompt": workflow, "client_id": self.client_id}, timeout=30)
        if response.status_code != 200:
            print(f"      ❌ ComfyUI rejected the workflow: {response.text[:800]}")
            return None
        prompt_id = response.json()["prompt_id"]

        entry = self._wait_for(prompt_id)
        if not entry:
            return None

        outputs = [item
                   for node_output in entry.get("outputs", {}).values()
                   for items in node_output.values() if isinstance(items, list)
                   for item in items
                   if isinstance(item, dict) and str(item.get("filename", "")).lower().endswith(VIDEO_EXTENSIONS)]
        if not outputs:
            print("      ❌ The workflow finished without saving a video.")
            return None

        video = outputs[-1]
        ext = os.path.splitext(video["filename"])[1]
        save_path = os.path.join(self.clips_dir, f"{name}{ext}")
        params = {"filename": video["filename"], "subfolder": video.get("subfolder", ""), "type": video.get("type", "output")}
        with requests.get(f"{self.url}/view", params=params, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(save_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        print(f"      ✅ Saved {os.path.basename(save_path)}")
        return save_path

    def _wait_for(self, prompt_id):
        """Polls until the prompt finishes. Returns its history entry, or None on error/timeout."""
        started = time.time()
        while time.time() - started < self.timeout:
            entry = requests.get(f"{self.url}/history/{prompt_id}", timeout=30).json().get(prompt_id)
            if entry:
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    for kind, data in status.get("messages", []):
                        if kind == "execution_error":
                            print(f"      ❌ {data.get('node_type')}: {data.get('exception_message', '').strip()}")
                    return None
                if status.get("completed", True):
                    print(f"      ⏱️ Rendered in {time.time() - started:.0f}s")
                    return entry
            time.sleep(POLL_INTERVAL)

        print(f"      ❌ Timed out after {self.timeout}s. Cancelling.")
        try:
            requests.post(f"{self.url}/queue", json={"delete": [prompt_id]}, timeout=10)
            requests.post(f"{self.url}/interrupt", json={"prompt_id": prompt_id}, timeout=10)
        except requests.RequestException:
            pass
        return None

    def _upload(self, path, name):
        """Uploads to ComfyUI's input folder and returns the value LoadImage / LoadAudio expect."""
        with open(path, "rb") as f:
            response = requests.post(f"{self.url}/upload/image",
                                     files={"image": (name, f, "application/octet-stream")},
                                     data={"overwrite": "true"}, timeout=60)
        response.raise_for_status()
        data = response.json()
        return f"{data['subfolder']}/{data['name']}" if data.get("subfolder") else data["name"]

    def _portrait_crop(self, path):
        """
        Crops the reference image to the render aspect ratio around AVATAR_FOCUS_X.
        The workflow would otherwise centre-crop it, cutting off an off-centre presenter.
        """
        crop_path = os.path.join(self.clips_dir, "avatar_ref.png")
        aspect = self.width / self.height
        (
            ffmpeg.input(path)
            .filter('crop',
                    f'min(iw,ih*{aspect})', f'min(ih,iw/{aspect})',
                    f'max(0,min(iw-ow,iw*{self.focus_x}-ow/2))', '(ih-oh)/2')
            .output(crop_path, vframes=1)
            .run(overwrite_output=True, quiet=True)
        )
        return crop_path

    def _narration(self, scene, seconds):
        """The scene's voiceover padded with silence to the clip length, so audio and video latents match."""
        voice_path = os.path.join(self.clips_dir, f"avatar_voice_{scene['id']}.wav")
        (
            ffmpeg.input(scene['audio_path'])
            .filter('apad', whole_dur=seconds)
            .output(voice_path, t=seconds, ac=1, ar=48000)
            .run(overwrite_output=True, quiet=True)
        )
        return voice_path

    def _reference_from(self, clip):
        frame_path = os.path.join(self.clips_dir, "avatar_ref.png")
        ffmpeg.input(clip, ss=0.5).output(frame_path, vframes=1).run(overwrite_output=True, quiet=True)
        return self._upload(frame_path, "aishorts_avatar_frame.png")
