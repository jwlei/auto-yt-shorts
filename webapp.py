"""
Web UI for Auto YT Shorts — the control panel for the whole pipeline.

Starts generation jobs with every pipeline option, streams their log, shows
whether ComfyUI is up, surfaces recent API errors, and lists the finished
(0_output) and uploaded (1_uploaded) videos for playback and download — from
the desktop or a phone on the same network.

    python webapp.py --host 0.0.0.0 --port 8800
    (or double-click start_webapp.pyw / the "AutoShorts" desktop shortcut)

Set APP_PASSWORD to expose it beyond localhost. Without it the server refuses
any host but 127.0.0.1: an open instance lets anyone burn your Gemini and
stock video quota.
"""
import argparse
import json
import logging
import os
import re
import shutil
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from functools import wraps

import requests
import websocket
from dotenv import load_dotenv
from flask import (Flask, abort, redirect, render_template, request,
                   send_file, session, url_for)

from modules.api_log import summarize_api_error
from modules.avatar_generator import avatar_clip_range, on_screen_range
from modules.paths import OUTPUT_DIR, UPLOADED_DIR
from modules.quota import quota_summary
from modules.run_eta import RunEta
from modules.topic_history import read_history

load_dotenv()

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
FOLDERS = {"output": OUTPUT_DIR, "uploaded": UPLOADED_DIR}
STAGE_TIMINGS = os.path.join(PROJECT_ROOT, "logs", "stage_timings.json")
# Keeps the pipeline and nvidia-smi from flashing a console window when the
# server itself runs windowless (start_webapp.pyw).
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
API_ERROR_LOG = os.path.join(PROJECT_ROOT, "logs", "api_errors.log")
# start_webapp.pyw compares this with the code on disk to replace a server that
# predates an update (templates are cached and Python only loads at start).
STARTED_AT = time.time()

# Gemini voices only: edge-tts is never used.
VOICES = [
    ("", "Default for the language (UK: Presenter, female)"),
    ("en-gb-tutor-13", "Presenter — UK female, Winchester"),
    ("en-gb-storyteller-3", "Storyteller — UK female, Newcastle"),
    ("en-gb-storyteller-2", "Storyteller — UK female, mature"),
    ("en-gb-storyteller-1", "Storyteller — UK male"),
    ("en-us-storyteller-6", "Storyteller — US female"),
    ("en-us-storyteller-14", "Storyteller — US male"),
]
AVATAR_STYLES = {
    "lipsync": os.path.join(PROJECT_ROOT, "comfyui", "avatar_lipsync.json"),
    "reaction": os.path.join(PROJECT_ROOT, "comfyui", "avatar_workflow.json"),
}
# The panel's sliders: form field prefix, the variables they set, and their limits.
AVATAR_SLIDERS = (
    ("avatar_clips", "AVATAR_CLIPS_MIN", "AVATAR_CLIPS_MAX", 1, 9),
    ("avatar_seconds", "AVATAR_MIN_SECONDS", "AVATAR_MAX_SECONDS", 2, 10),
)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)


class Job:
    """
    Tracks the one run allowed at a time. Rendering saturates the CPU and GPU,
    so a second concurrent run would only make both slower.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.process = None
        # {"n": sequence number, "t": "HH:MM:SS", "text": ...}; the page appends by n.
        self.lines = []
        self.seq = 0
        self.started_at = None
        self.finished_at = None
        self.returncode = None
        self.command = None
        self.progress = {}
        self.eta = None

    @property
    def running(self):
        return self.process is not None and self.process.poll() is None

    def start(self, argv, env=None):
        with self.lock:
            if self.running:
                return False
            self.lines = []
            self.started_at = datetime.now()
            self.finished_at = None
            self.returncode = None
            self.command = " ".join(argv[1:])
            self.progress = {}
            self.eta = RunEta(argv, env, STAGE_TIMINGS)
            self.process = subprocess.Popen(
                argv,
                cwd=PROJECT_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                # Unbuffered child output, otherwise the log only appears at the end.
                env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", **(env or {})},
                creationflags=NO_WINDOW,
                # Its own process group, so stop() can take ffmpeg down with it.
                start_new_session=os.name != "nt",
            )

        threading.Thread(target=self._drain, daemon=True).start()
        return True

    def _drain(self):
        process = self.process
        for line in process.stdout:
            line = line.rstrip()
            # ffmpeg's progress carriage-returns would flood the log.
            if not line or line.startswith("frame=") or line.startswith("["):
                continue
            self._add(line)
            self._track(line)
            # Keep memory bounded on long batches.
            if len(self.lines) > 2000:
                del self.lines[:500]
        process.wait()
        if process.returncode == 0:
            self.eta.finish()
        self.returncode = process.returncode
        self.finished_at = datetime.now()

    def _add(self, text):
        self.seq += 1
        self.lines.append({"n": self.seq, "t": datetime.now().strftime("%H:%M:%S"), "text": text})

    # Lines main.py and its modules print, turned into structured progress.
    PATTERNS = [
        ("run", re.compile(r"===== RUN (\d+)/(\d+) =====")),
        ("stage", re.compile(r"\u25b6 Stage (\d+)/(\d+): (.+)")),
        ("scenes", re.compile(r"narration for (\d+) scenes|Generation for (\d+) scenes")),
        ("clip", re.compile(r"Clip (\d+)/(\d+) \u00b7 scene (\d+): (\d+)s ([\w-]+)")),
        ("rejected", re.compile(r"mouth barely moves \(.*attempt (\d+)/(\d+)\)")),
        ("render", re.compile(r"Processing Scene (\d+)")),
    ]

    def _track(self, line):
        p = self.progress
        for kind, pattern in self.PATTERNS:
            m = pattern.search(line)
            if not m:
                continue
            if kind == "run":
                p["run"] = [int(m.group(1)), int(m.group(2))]
                self.eta.video(int(m.group(1)), int(m.group(2)))
            elif kind == "stage":
                p["stage"] = [int(m.group(1)), int(m.group(2)), m.group(3).strip()]
                p["stage_started"] = time.time()
                p.pop("detail", None)
                self.eta.stage(*p["stage"])
            elif kind == "scenes":
                p["scenes"] = int(m.group(1) or m.group(2))
                self.eta.scenes(p["scenes"])
            elif kind == "clip":
                p["detail"] = f"Clip {m.group(1)} of {m.group(2)} - scene {m.group(3)}, {m.group(4)}s {m.group(5)}"
                self.eta.clip(int(m.group(1)), int(m.group(2)))
            elif kind == "rejected":
                self.eta.clip_rejected(int(m.group(1)), int(m.group(2)))
            elif kind == "render":
                total = p.get("scenes")
                p["detail"] = f"Scene {m.group(1)}" + (f" of {total}" if total else "")
                self.eta.scene()
            return

    def stop(self):
        """
        Kills the run and every process it started: terminate() alone would
        leave its ffmpeg renders running.
        """
        with self.lock:
            if not self.running:
                return False
            pid = self.process.pid
            if os.name == "nt":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                               capture_output=True, creationflags=NO_WINDOW)
            else:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            self._add("🛑 Stopped by user.")
        interrupt_comfyui()
        return True

    def snapshot(self):
        return {
            "running": self.running,
            "lines": list(self.lines),
            "returncode": self.returncode,
            "command": self.command,
            "started_at": self.started_at.strftime("%H:%M:%S") if self.started_at else None,
            "elapsed": int(((self.finished_at or datetime.now()) - self.started_at).total_seconds()) if self.started_at else 0,
            "progress": {**self.progress,
                         "stage_elapsed": int(time.time() - self.progress["stage_started"])
                         if "stage_started" in self.progress else 0,
                         "eta": self.eta.estimate() if self.eta and self.running else None},
        }


job = Job()


def password_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not os.getenv("APP_PASSWORD") or session.get("authed"):
            return view(*args, **kwargs)
        return redirect(url_for("login", next=request.path))
    return wrapper


def list_videos(folder):
    directory = FOLDERS[folder]
    if not os.path.isdir(directory):
        return []

    try:
        topics = {entry.get("video"): entry["topic"] for entry in read_history()}
    except (OSError, ValueError):
        topics = {}  # A broken history only costs the topic captions

    videos = []
    for name in os.listdir(directory):
        if not name.endswith(".mp4"):
            continue
        stat = os.stat(os.path.join(directory, name))
        videos.append({
            "name": name,
            "topic": topics.get(name),
            "size_mb": round(stat.st_size / (1024 * 1024), 1),
            "created": datetime.fromtimestamp(stat.st_mtime).strftime("%d/%m %H:%M"),
            "mtime": stat.st_mtime,
        })

    videos.sort(key=lambda v: v["mtime"], reverse=True)
    return videos


def safe_video_path(folder, name):
    """
    Resolves name inside one of the video folders, refusing anything that escapes it.
    """
    directory = FOLDERS.get(folder)
    if not directory:
        abort(404)
    path = os.path.abspath(os.path.join(directory, name))
    if os.path.dirname(path) != os.path.abspath(directory) or not os.path.exists(path):
        abort(404)
    return path


def lan_ip():
    """The address other devices on the home network reach this PC at."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 80))  # No packet is sent; this only picks the outgoing interface.
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


_comfyui_cache = {"at": 0.0, "online": False}
_comfyui_start_lock = threading.Lock()
_comfyui_start_requested_at = 0.0
# Seconds a run waits for a ComfyUI that Generate just launched to load its models.
COMFYUI_STARTUP_WAIT = 180


def comfyui_online():
    # Cached briefly: every open browser polls status every 2s.
    if time.time() - _comfyui_cache["at"] > 10:
        url = (os.getenv("COMFYUI_URL") or "http://127.0.0.1:8188").rstrip("/")
        try:
            _comfyui_cache["online"] = requests.get(f"{url}/system_stats", timeout=2).status_code == 200
        except requests.RequestException:
            _comfyui_cache["online"] = False
        _comfyui_cache["at"] = time.time()
    return _comfyui_cache["online"]


# What each node of the LTX workflow is doing, in plain words.
COMFY_LABELS = {
    "UNETLoader": "Loading the video model", "UnetLoaderGGUF": "Loading the video model",
    "DualCLIPLoader": "Loading the text encoder", "DualCLIPLoaderGGUF": "Loading the text encoder",
    "VAELoader": "Loading the VAE", "VAELoaderKJ": "Loading the audio VAE",
    "LatentUpscaleModelLoader": "Loading the upscaler",
    "LoadImage": "Loading the reference image", "LoadAudio": "Loading the narration",
    "CLIPTextEncode": "Reading the prompt", "LTXVAudioVAEEncode": "Encoding the narration",
    "LTXVImgToVideoInplace": "Placing the reference image", "LTXVPreprocess": "Preparing the image",
    "SamplerCustomAdvanced": "Generating video", "LTXVLatentUpsampler": "Upscaling",
    "VAEDecodeTiled": "Decoding frames", "LTXVAudioVAEDecode": "Decoding audio",
    "VHS_VideoCombine": "Saving the video",
}


class ComfyMonitor:
    """
    Listens on ComfyUI's websocket (as the pipeline's client id, since ComfyUI
    only sends progress to the client that queued the job) and keeps the latest
    state: which node is running, sampling steps, queue size.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"connected": False, "queue": 0}
        self.nodes = {}
        self.samplers_seen = []
        threading.Thread(target=self._loop, daemon=True).start()

    def _base(self):
        return (os.getenv("COMFYUI_URL") or "http://127.0.0.1:8188").rstrip("/")

    def _loop(self):
        client_id = os.getenv("COMFYUI_CLIENT_ID") or "aishorts"
        while True:
            url = self._base().replace("http", "ws", 1) + f"/ws?clientId={client_id}"
            try:
                ws = websocket.create_connection(url, timeout=10)
                ws.settimeout(None)
                with self.lock:
                    self.state["connected"] = True
                while True:
                    message = ws.recv()
                    if isinstance(message, str):  # Binary frames are sampler previews
                        self._handle(json.loads(message))
            except Exception:
                with self.lock:
                    self.state = {"connected": False, "queue": 0}
                time.sleep(5)

    def _load_nodes(self, prompt_id):
        """Node id -> class for the running prompt, from ComfyUI's queue."""
        try:
            queue = requests.get(f"{self._base()}/queue", timeout=5).json()
            for entry in queue.get("queue_running", []):
                if entry[1] == prompt_id:
                    return {k: v.get("class_type", "") for k, v in entry[2].items()}
        except Exception:
            pass
        return {}

    def _handle(self, msg):
        kind, data = msg.get("type"), msg.get("data") or {}
        with self.lock:
            s = self.state
            if kind == "status":
                s["queue"] = data.get("status", {}).get("exec_info", {}).get("queue_remaining", 0)
            elif kind == "execution_start":
                self.nodes = self._load_nodes(data.get("prompt_id"))
                self.samplers_seen = []
                s.update(active=True, started=time.time(), node=None, label="Starting", value=None, max=None)
            elif kind == "executing":
                node = data.get("node")
                if node is None:
                    s.update(active=False, label="Idle", node=None, value=None, max=None)
                    return
                cls = self.nodes.get(str(node), "")
                label = COMFY_LABELS.get(cls, cls or f"Node {node}")
                if cls == "SamplerCustomAdvanced":
                    if node not in self.samplers_seen:
                        self.samplers_seen.append(node)
                    passes = sum(1 for c in self.nodes.values() if c == cls)
                    label += f" - pass {self.samplers_seen.index(node) + 1} of {passes}"
                s.update(active=True, node=node, label=label, value=None, max=None)
            elif kind == "progress":
                s.update(value=data.get("value"), max=data.get("max"))
            elif kind in ("execution_success", "execution_error", "execution_interrupted"):
                s.update(active=False, label="Idle" if kind == "execution_success" else kind.replace("_", " "),
                         node=None, value=None, max=None)

    def snapshot(self):
        with self.lock:
            s = dict(self.state)
        if s.get("started") and s.get("active"):
            s["elapsed"] = int(time.time() - s["started"])
        s.pop("started", None)
        return s


comfy_monitor = None
_gpu_cache = {"at": 0.0, "value": None}


def gpu_stats():
    """Utilisation / VRAM / power from nvidia-smi, cached for a few seconds."""
    if time.time() - _gpu_cache["at"] < 3:
        return _gpu_cache["value"]
    _gpu_cache["at"] = time.time()
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,power.draw,temperature.gpu",
             "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5,
            creationflags=NO_WINDOW,
        ).stdout.strip().splitlines()[0]
        util, used, total, power, temp = [x.strip() for x in out.split(",")]
        _gpu_cache["value"] = {"util": int(float(util)), "mem_used": round(float(used) / 1024, 1),
                               "mem_total": round(float(total) / 1024, 1),
                               "power": int(float(power)), "temp": int(float(temp))}
    except Exception:
        _gpu_cache["value"] = None
    return _gpu_cache["value"]


def comfyui_launcher():
    path = os.getenv("COMFYUI_LAUNCHER")
    return path if path and os.path.exists(path) else None


def interrupt_comfyui():
    """
    Cancels the avatar clip ComfyUI is rendering for a stopped run, and any
    queued after it. ComfyUI itself keeps running, with its models loaded for
    the next run.
    """
    if not (comfy_monitor and comfy_monitor.snapshot().get("active")):
        return
    url = (os.getenv("COMFYUI_URL") or "http://127.0.0.1:8188").rstrip("/")
    try:
        # Clear the queue first, or the next queued clip starts as soon as this one stops.
        requests.post(f"{url}/queue", json={"clear": True}, timeout=5)
        requests.post(f"{url}/interrupt", timeout=5)
    except requests.RequestException:
        pass


def comfyui_command(launcher):
    """
    The ComfyUI command line from a start script such as the portable build's
    run_nvidia_gpu.bat, with --disable-auto-launch added so no browser tab
    opens: the panel only needs its API. None if the script has no main.py line.
    """
    try:
        with open(launcher, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if "main.py" in line and not line.lower().startswith(("rem", "::", "echo")):
                    return line if "--disable-auto-launch" in line else line + " --disable-auto-launch"
    except OSError:
        pass
    return None


def launch_comfyui(launcher):
    """
    Starts ComfyUI in its own minimised console window, so it can be watched
    (or closed to stop it) and outlives this server. (Not 'start': it only takes
    a quoted first argument as the window title and would open a folder named ComfyUI.)
    """
    command = comfyui_command(launcher)
    if os.name != "nt":
        subprocess.Popen(command or [launcher], shell=bool(command),
                         cwd=os.path.dirname(launcher), start_new_session=True)
        return
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 7  # SW_SHOWMINNOACTIVE: minimised, without stealing focus
    subprocess.Popen(
        f"cmd /c {command}" if command else ["cmd", "/c", launcher],
        cwd=os.path.dirname(launcher),
        creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP,
        startupinfo=startup,
    )


def start_comfyui_for_run(form):
    """
    Starts ComfyUI for avatar runs when a launcher is configured. Returns True
    when ComfyUI is (still) booting, so the run should wait for it.
    """
    global _comfyui_start_requested_at
    if (form.get("avatar_source") or "comfyui") != "comfyui" or (form.get("avatar_clips") or "auto") == "0":
        return False

    with _comfyui_start_lock:
        _comfyui_cache["at"] = 0.0
        if comfyui_online():
            return False
        launcher = comfyui_launcher()
        if not launcher:
            return False
        if time.time() - _comfyui_start_requested_at < COMFYUI_STARTUP_WAIT:
            return True  # Launched moments ago and still loading.
        try:
            launch_comfyui(launcher)
        except OSError as e:
            raise RuntimeError(f"Could not start ComfyUI: {e}") from e
        _comfyui_start_requested_at = time.time()
        _comfyui_cache["at"] = 0.0
        return True


def recent_api_errors(count=12):
    try:
        with open(API_ERROR_LOG, "r", encoding="utf-8") as f:
            lines = f.readlines()[-count:]
    except OSError:
        return []
    errors = []
    for line in reversed(lines):
        parts = line.rstrip("\n").split("\t")
        if len(parts) >= 5:
            errors.append({"when": parts[0].replace("T", " "), "service": parts[1], "model": parts[2],
                           "status": parts[3], "message": summarize_api_error(parts[4])})
    return errors


_quota_cache = {"at": 0.0, "value": None}


def cached_quota():
    # Cached briefly: every open browser polls status every 2s.
    if time.time() - _quota_cache["at"] > 5:
        _quota_cache["value"] = quota_summary()
        _quota_cache["at"] = time.time()
    return _quota_cache["value"]


@app.route("/login", methods=["GET", "POST"])
def login():
    expected = os.getenv("APP_PASSWORD")
    if not expected:
        return redirect(url_for("index"))

    error = None
    if request.method == "POST":
        # Constant-time compare so the password cannot be guessed by timing.
        if secrets.compare_digest(request.form.get("password", ""), expected):
            session["authed"] = True
            session.permanent = True
            return redirect(request.args.get("next") or url_for("index"))
        error = "Wrong password."

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@password_required
def index():
    port = request.host.rsplit(":", 1)[-1] if ":" in request.host else "80"
    return render_template(
        "index.html",
        voices=VOICES,
        protected=bool(os.getenv("APP_PASSWORD")),
        phone_url=f"http://{lan_ip()}:{port}",
        defaults={
            "avatar_source": (os.getenv("AVATAR_SOURCE") or "static").lower(),
            "avatar_clips": (os.getenv("AVATAR_CLIPS") or "auto").lower(),
            "avatar_clips_min": avatar_clip_range()[0],
            "avatar_clips_max": avatar_clip_range()[1],
            "avatar_seconds_min": on_screen_range()[0],
            "avatar_seconds_max": on_screen_range()[1],
        },
        static_avatar=os.path.exists(os.path.join(PROJECT_ROOT, "assets", "avatar", "avatars.mp4")),
        can_upload=youtube_configured(),
    )


def build_run(form):
    """
    Turns the form into a main.py command line plus environment overrides.
    Returns (argv, env, error).
    """
    argv = [sys.executable, "main.py"]
    env = {}

    topic = (form.get("topic") or "").strip()
    if topic:
        argv += ["--topic", topic]

    try:
        runs = max(1, min(10, int(form.get("runs", 1))))
    except ValueError:
        runs = 1
    if topic and runs > 1:
        return None, None, "A topic makes one video. Leave Topic empty to make several."
    argv += ["--runs", str(runs)]

    lang = form.get("lang") or "en-GB"
    if lang not in ("en-GB", "en-US"):
        return None, None, "Unknown language."
    argv += ["--lang", lang]

    voice = form.get("voice") or ""
    if voice not in dict(VOICES):
        return None, None, "Unknown voice."
    if voice:
        argv += ["--voice", voice]

    clips = form.get("avatar_clips") or "auto"
    if clips not in ("auto", "0", "2", "3"):
        return None, None, "Unknown avatar layout."
    argv += ["--avatar-clips", clips]

    source = form.get("avatar_source") or "comfyui"
    if source not in ("comfyui", "static"):
        return None, None, "Unknown avatar source."
    env["AVATAR_SOURCE"] = source

    style = form.get("avatar_style") or "lipsync"
    if style not in AVATAR_STYLES:
        return None, None, "Unknown avatar style."
    env["COMFYUI_WORKFLOW"] = AVATAR_STYLES[style]

    for field, low_name, high_name, lowest, highest in AVATAR_SLIDERS:
        try:
            low, high = int(form[f"{field}_min"]), int(form[f"{field}_max"])
        except KeyError:
            continue  # A page opened before the sliders existed: keep the .env values
        except ValueError:
            return None, None, "Avatar sliders must be whole numbers."
        if not lowest <= low <= high <= highest:
            return None, None, f"Avatar slider out of range: {low}-{high}, allowed {lowest}-{highest}."
        env[low_name], env[high_name] = str(low), str(high)

    if form.get("upload"):
        privacy = form.get("privacy") or "private"
        if privacy not in ("private", "unlisted", "public"):
            return None, None, "Unknown privacy setting."
        argv += ["--upload", "--privacy", privacy]

    if form.get("keep_cache"):
        argv.append("--keep-cache")
    if form.get("fail_fast"):
        argv.append("--fail-fast")

    output = (form.get("output") or "").strip()
    if output:
        if runs != 1:
            return None, None, "A custom file name only works for a single video."
        if not output.lower().endswith(".mp4"):
            output += ".mp4"
        if not re.fullmatch(r"[\w.-]+\.mp4", output):
            return None, None, "File name may only contain letters, numbers, '-', '_' and '.'."
        argv += ["--output", output]

    return argv, env, None


@app.route("/generate", methods=["POST"])
@password_required
def generate():
    if job.running:
        return {"ok": False, "error": "A run is already in progress."}, 409

    argv, env, error = build_run(request.form)
    if error:
        return {"ok": False, "error": error}, 400

    try:
        if start_comfyui_for_run(request.form):
            env["COMFYUI_STARTUP_WAIT"] = str(COMFYUI_STARTUP_WAIT)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}, 500

    if not job.start(argv, env):
        return {"ok": False, "error": "A run is already in progress."}, 409
    return {"ok": True}


@app.route("/check", methods=["POST"])
@password_required
def check():
    """Runs the preflight with the same options as a real run would use."""
    if job.running:
        return {"ok": False, "error": "A run is already in progress."}, 409
    argv, env, error = build_run(request.form)
    if error:
        return {"ok": False, "error": error}, 400
    argv.append("--check")
    job.start(argv, env)
    return {"ok": True}


@app.route("/stop", methods=["POST"])
@password_required
def stop():
    return {"ok": job.stop()}


@app.route("/shutdown", methods=["POST"])
@password_required
def shutdown():
    """Stops the server itself: started from start_webapp.pyw it has no window to close."""
    if job.running:
        return {"ok": False, "error": "A run is in progress. Stop it first."}, 409
    # Reply first, then exit, so the browser gets its answer.
    threading.Timer(0.5, os._exit, args=(0,)).start()
    return {"ok": True}


@app.route("/launcher/refresh", methods=["POST"])
def launcher_refresh():
    """
    Exits if the code changed after this server started, so start_webapp.pyw can
    start a fresh one. Only answers this PC, and never stops a run.
    """
    if request.remote_addr not in ("127.0.0.1", "::1"):
        abort(403)
    try:
        code_changed_at = float(request.form.get("code_changed_at", 0))
    except ValueError:
        abort(400)
    if code_changed_at <= STARTED_AT or job.running:
        return {"restarting": False}
    threading.Timer(0.5, os._exit, args=(0,)).start()
    return {"restarting": True}


@app.route("/api/status")
@password_required
def status():
    payload = job.snapshot()
    payload["videos"] = {folder: list_videos(folder) for folder in FOLDERS}
    online = comfyui_online()
    payload["comfyui"] = {"online": online, "can_start": bool(comfyui_launcher()),
                          "starting": not online and time.time() - _comfyui_start_requested_at < COMFYUI_STARTUP_WAIT,
                          **(comfy_monitor.snapshot() if comfy_monitor else {})}
    payload["gpu"] = gpu_stats()
    payload["api_errors"] = recent_api_errors()
    payload["quota"] = cached_quota()
    return payload


@app.route("/videos/<folder>/<path:name>")
@password_required
def video(folder, name):
    return send_file(safe_video_path(folder, name), mimetype="video/mp4", conditional=True)


@app.route("/download/<folder>/<path:name>")
@password_required
def download(folder, name):
    return send_file(safe_video_path(folder, name), as_attachment=True, download_name=name)


def youtube_configured():
    return all(os.getenv(k) for k in ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"))


@app.route("/publish", methods=["POST"])
@password_required
def publish():
    """Uploads one video from 0_output (form 'video'), or all of them, as a run."""
    privacy = request.form.get("privacy") or "private"
    if privacy not in ("private", "unlisted", "public"):
        return {"ok": False, "error": "Unknown privacy setting."}, 400
    if not youtube_configured():
        return {"ok": False, "error": "Set the YouTube keys in .env first (run authorize_youtube.py once)."}, 400

    argv = [sys.executable, "publish.py", "--privacy", privacy]
    name = request.form.get("video")
    if name:
        argv.append(os.path.basename(safe_video_path("output", name)))
        topic = (request.form.get("topic") or "").strip()
        if topic:
            argv += ["--topic", topic]
    else:
        argv.append("--all")

    if not job.start(argv):
        return {"ok": False, "error": "A run is already in progress."}, 409
    return {"ok": True}


@app.route("/delete/<folder>/<path:name>", methods=["POST"])
@password_required
def delete(folder, name):
    os.remove(safe_video_path(folder, name))
    return {"ok": True}


class _QuietPolling(logging.Filter):
    """Drops the access-log line for the page's 2-second status poll, which would bury everything else."""

    def filter(self, record):
        return "/api/status" not in record.getMessage()


def main():
    logging.getLogger("werkzeug").addFilter(_QuietPolling())

    parser = argparse.ArgumentParser(description="Web UI for Auto YT Shorts.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1; 0.0.0.0 for your phone).")
    parser.add_argument("--port", type=int, default=8800, help="Port (default: 8800).")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.host != "127.0.0.1" and not os.getenv("APP_PASSWORD"):
        parser.error(
            "Refusing to listen on a public address without APP_PASSWORD set. "
            "Anyone who finds the URL could spend your Gemini and stock video quota. "
            "Set APP_PASSWORD in your .env and try again."
        )

    if not os.getenv("FLASK_SECRET_KEY"):
        print("⚠️ FLASK_SECRET_KEY not set — using a random key, so logins drop on restart.")

    global comfy_monitor
    comfy_monitor = ComfyMonitor()

    print(f"🌐 This PC:    http://127.0.0.1:{args.port}")
    if args.host == "0.0.0.0":
        print(f"📱 Your phone: http://{lan_ip()}:{args.port}  (or http://{socket.gethostname()}.local:{args.port})")
    print("   Stop it with 'Shut down' in the panel (or close this window if it has one).")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
