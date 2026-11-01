import os
import re
import time
import base64
import asyncio
import edge_tts
import ffmpeg
import mutagen
import requests
from dotenv import load_dotenv

from modules.api_log import is_daily_quota, log_api_call, log_api_error
from modules.gemini_models import get_tts_models

load_dotenv()

# Voice engine, chosen with TTS_PROVIDER in .env: 'gemini' (natural, default) or 'edge' (free, robotic).
# edge-tts is never used as a fallback: if every Gemini model fails, the run fails.
TTS_PROVIDERS = ("gemini", "edge")
DEFAULT_TTS_PROVIDER = "gemini"
INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"

# Default narrators from Gemini's voice library (en-GB: female presenter, Winchester accent).
GEMINI_VOICES = {"en-GB": "en-gb-tutor-13", "en-US": "en-us-storyteller-14"}
EDGE_VOICES = {"en-GB": "en-GB-SoniaNeural", "en-US": "en-US-AvaNeural"}

# Delivery shared by every scene; the scene's mood from the script is appended to it.
DEFAULT_STYLE = "Engaging YouTube Shorts documentary narrator. Natural and conversational, brisk pace, clear diction"

# The whole narration normally comes back in 5-20s; a reply this late means the
# model is stuck, not slow: move on to the next model in the chain.
DEFAULT_GEMINI_TTS_TIMEOUT = 120
# Free tier allows 3 requests/minute per TTS model. Only matters for the per-scene
# fallback; the normal path makes one request per video.
DEFAULT_GEMINI_TTS_RPM = 3
# Per-minute 429s are waited out this many times before giving up on a model.
RATE_LIMIT_RETRIES = 3

# Silence detection used to split the single narration take back into scenes.
SILENCE_NOISE = "-35dB"
SILENCE_MIN = 0.2


class GeminiTTSError(Exception):
    def __init__(self, model, status_code, message):
        super().__init__(f"{status_code}: {message}")
        self.model = model
        self.status_code = status_code


class NarrationSplitError(Exception):
    """The single take could not be cut into scenes reliably."""


def get_tts_provider():
    """Returns the configured voice engine, or raises if TTS_PROVIDER is unknown."""
    provider = (os.getenv("TTS_PROVIDER") or DEFAULT_TTS_PROVIDER).strip().lower()
    if provider not in TTS_PROVIDERS:
        raise RuntimeError(f"TTS_PROVIDER='{provider}' is not supported. Use one of: {', '.join(TTS_PROVIDERS)}.")
    return provider


def _retry_after(message):
    """Seconds from a 'Please retry in 1m5s' / 'retry in 20.5s' message, or None."""
    match = re.search(r"retry in ((?:\d+h)?(?:\d+m)?(?:[\d.]+s)?)", message)
    if not match or not match.group(1):
        return None
    total = 0.0
    for value, unit in re.findall(r"([\d.]+)([hms])", match.group(1)):
        total += float(value) * {"h": 3600, "m": 60, "s": 1}[unit]
    return total


def _find_audio(data):
    """Returns the base64 audio from an interactions response (last model output wins)."""
    for step in reversed(data.get("steps") or []):
        if step.get("type") != "model_output":
            continue
        for item in reversed(step.get("content") or []):
            if item.get("type") == "audio" and item.get("data"):
                return item["data"]
    # Response shapes move between API versions: fall back to any audio block.
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("type") == "audio" and node.get("data"):
                return node["data"]
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def _pick_boundaries(mids, weights, expected):
    """
    Chooses one silence per expected scene boundary, in order, minimising the
    distance from where each boundary should fall. Longer pauses are preferred,
    since the narrator pauses longest between scenes.
    """
    k_count, s_count = len(expected), len(mids)
    if s_count < k_count:
        return None

    def cost(k, j):
        return abs(mids[j] - expected[k]) - 0.3 * weights[j]

    inf = float("inf")
    best = [[inf] * s_count for _ in range(k_count)]
    prev = [[-1] * s_count for _ in range(k_count)]
    for j in range(s_count):
        best[0][j] = cost(0, j)
    for k in range(1, k_count):
        running, running_i = inf, -1
        for j in range(s_count):
            if j > 0 and best[k - 1][j - 1] < running:
                running, running_i = best[k - 1][j - 1], j - 1
            if running_i >= 0:
                best[k][j] = running + cost(k, j)
                prev[k][j] = running_i

    j = min(range(s_count), key=lambda x: best[k_count - 1][x])
    if best[k_count - 1][j] == inf:
        return None
    picks = []
    for k in range(k_count - 1, -1, -1):
        picks.append(j)
        j = prev[k][j]
    return [mids[i] for i in reversed(picks)]


class AudioEngine:
    def __init__(self, voice=None, lang="en-GB", output_dir=None):
        self.provider = get_tts_provider()
        # edge-tts voice names end in 'Neural'; anything else is a Gemini voice.
        # A voice meant for the other engine is ignored, so --voice never breaks either one.
        is_edge_voice = bool(voice) and voice.endswith("Neural")
        self.edge_voice = voice if is_edge_voice else EDGE_VOICES.get(lang, EDGE_VOICES["en-GB"])
        self.gemini_voice = (os.getenv("GEMINI_TTS_VOICE")
                             or (voice if voice and not is_edge_voice else None)
                             or GEMINI_VOICES.get(lang, GEMINI_VOICES["en-GB"]))
        self.style = os.getenv("GEMINI_TTS_STYLE") or DEFAULT_STYLE
        self.gemini_models = get_tts_models()
        self.timeout = int(os.getenv("GEMINI_TTS_TIMEOUT") or DEFAULT_GEMINI_TTS_TIMEOUT)
        self.min_interval = 60.0 / max(1, int(os.getenv("GEMINI_TTS_RPM") or DEFAULT_GEMINI_TTS_RPM))
        self.last_request = {}
        self.output_dir = output_dir or os.path.join(os.getcwd(), "assets", "audio_clips")
        os.makedirs(self.output_dir, exist_ok=True)

    # --- Gemini TTS ---

    def _gemini_request(self, text, style, model):
        """
        One TTS request over plain REST. The SDK silently honours the server's
        retry-after, which for a used-up daily quota means sleeping for hours;
        here every error is logged and surfaced at once. Per-minute 429s are
        waited out; a daily-quota 429, a timeout or any other error raises so
        the caller moves on to the next model.
        """
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not set. Create a .env file or set the environment variable before running.")
        body = {
            "model": model,
            "input": [{"type": "user_input", "content": [{
                "type": "text",
                "text": text,
                "annotations": [{"type": "speech_metadata", "style": style}],
            }]}],
            "response_format": {"type": "audio"},
            "generation_config": {"speech_config": [{"voice": self.gemini_voice}]},
        }

        for attempt in range(RATE_LIMIT_RETRIES + 1):
            # Stay under the requests-per-minute limit instead of tripping it.
            wait = self.last_request.get(model, 0) + self.min_interval - time.time()
            if wait > 0:
                time.sleep(wait)
            self.last_request[model] = time.time()

            try:
                response = requests.post(INTERACTIONS_URL, headers={"x-goog-api-key": api_key},
                                         json=body, timeout=(10, self.timeout))
            except requests.Timeout:
                error = GeminiTTSError(model, "timeout", f"no response within {self.timeout}s")
                log_api_error("Gemini TTS", model, error)
                raise error
            except requests.RequestException as e:
                log_api_error("Gemini TTS", model, e)
                raise

            if response.status_code == 200:
                audio = _find_audio(response.json())
                if not audio:
                    error = GeminiTTSError(model, 200, "response contained no audio")
                    log_api_error("Gemini TTS", model, error)
                    raise error
                log_api_call("Gemini TTS", model)
                return base64.b64decode(audio)

            try:
                message = response.json().get("error", {}).get("message") or response.text
            except ValueError:
                message = response.text
            error = GeminiTTSError(model, response.status_code, message)
            log_api_error("Gemini TTS", model, error)

            retryable = response.status_code == 429 or response.status_code >= 500
            if not retryable or is_daily_quota(message) or attempt == RATE_LIMIT_RETRIES:
                raise error
            delay = min(_retry_after(message) or 2 ** (attempt + 2), 90)
            print(f"      ⏳ {model} rate-limited. Waiting {delay:.0f}s...")
            time.sleep(delay)

    def _narrate_single_take(self, script_data, model):
        """
        Voices the whole script in ONE request, then cuts it into scenes at the
        pauses between them. One request per video instead of one per scene
        keeps a run inside the free tier's 10 requests/day, and the narration
        sounds like one continuous read.
        """
        moods = ", ".join(f"{i + 1}: {s['mood']}" for i, s in enumerate(script_data) if s.get('mood'))
        style = (f"{self.style}. Read each paragraph as its own beat, with a clear pause of about "
                 f"one second between paragraphs." + (f" Paragraph moods: {moods}." if moods else ""))
        text = "\n\n".join(scene['text'] for scene in script_data)

        take_path = os.path.join(self.output_dir, "narration_full.wav")
        with open(take_path, "wb") as f:
            f.write(self._gemini_request(text, style, model))
        total = self.get_audio_duration(take_path)
        if total <= 0:
            raise GeminiTTSError(model, 200, "returned unreadable audio")

        # Where each scene boundary should fall if the read were evenly paced.
        lengths = [len(scene['text']) for scene in script_data]
        expected_durations = [total * n / sum(lengths) for n in lengths]
        expected = []
        running = 0.0
        for d in expected_durations[:-1]:
            running += d
            expected.append(running)

        stderr = (
            ffmpeg.input(take_path)
            .filter('silencedetect', noise=SILENCE_NOISE, d=SILENCE_MIN)
            .output('-', format='null')
            .run(capture_stderr=True, quiet=True)[1]
            .decode('utf8', errors='replace')
        )
        starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", stderr)]
        ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", stderr)]
        silences = [(s, e) for s, e in zip(starts, ends) if 0.3 < s and e < total - 0.3]
        boundaries = _pick_boundaries([(s + e) / 2 for s, e in silences],
                                      [min(e - s, 1.0) for s, e in silences], expected)
        if boundaries is None:
            raise NarrationSplitError(f"found {len(silences)} pauses for {len(script_data)} scenes")

        cuts = [0.0] + boundaries + [total]
        for scene, start, end, wanted in zip(script_data, cuts, cuts[1:], expected_durations):
            # A boundary snapped to the wrong pause shifts a whole sentence, which shows up here.
            if abs((end - start) - wanted) > max(2.0, 0.6 * wanted):
                raise NarrationSplitError(f"scene {scene['id']} came out {end - start:.1f}s, expected ~{wanted:.1f}s")

        narrated = []
        for scene, start, end in zip(script_data, cuts, cuts[1:]):
            file_path = os.path.join(self.output_dir, f"voice_{scene['id']}.wav")
            ffmpeg.input(take_path, ss=start, t=end - start).output(file_path, acodec='pcm_s16le').run(overwrite_output=True, quiet=True)
            scene['audio_path'] = file_path
            scene['duration'] = self.get_audio_duration(file_path)
            narrated.append(scene)
            print(f"   ✅ Scene {scene['id']}: {scene['duration']:.2f}s")
        return narrated

    def _narrate_per_scene(self, script_data, model):
        """One request per scene, paced to the per-minute limit. Raises on any failure."""
        narrated = []
        for scene in script_data:
            style = f"{self.style}. Mood: {scene['mood']}" if scene.get('mood') else self.style
            file_path = os.path.join(self.output_dir, f"voice_{scene['id']}.wav")
            with open(file_path, "wb") as f:
                f.write(self._gemini_request(scene['text'], style, model))
            duration = self.get_audio_duration(file_path)
            if duration <= 0:
                raise GeminiTTSError(model, 200, f"scene {scene['id']} returned unreadable audio")
            scene['audio_path'] = file_path
            scene['duration'] = duration
            narrated.append(scene)
            print(f"   ✅ Scene {scene['id']}: {duration:.2f}s generated.")
        return narrated

    def _narrate_gemini(self, script_data):
        """
        Tries each model in the chain. A video is always voiced by a single model,
        so takes never mix. Raises if every model fails: the run fails rather than
        dropping to the robotic edge-tts voice.
        """
        errors = []
        for model in self.gemini_models:
            print(f"🎙️ Gemini narration for {len(script_data)} scenes (voice: {self.gemini_voice}, model: {model})...")
            try:
                try:
                    return self._narrate_single_take(script_data, model)
                except NarrationSplitError as e:
                    print(f"   ⚠️ Could not split the single take ({e}). Voicing scene by scene instead...")
                    return self._narrate_per_scene(script_data, model)
            except Exception as e:
                print(f"   ⚠️ {model} failed: {e}")
                errors.append(f"{model}: {e}")

        raise RuntimeError("Gemini TTS failed on every model — " + "; ".join(errors) + " (see logs/api_errors.log)")

    # --- edge-tts (only with TTS_PROVIDER=edge) ---

    async def _edge_tts(self, text, output_path, retries=3):
        """
        Generates an MP3 with edge-tts, with retry logic to handle connection drops.
        """
        for attempt in range(retries):
            try:
                # Rate +10% for engagement
                communicate = edge_tts.Communicate(text, self.edge_voice, rate="+10%")
                await communicate.save(output_path)
                return output_path

            except Exception as e:
                print(f"      ⚠️ Audio Error (Attempt {attempt+1}/{retries}): {e}")
                if attempt < retries - 1:
                    await asyncio.sleep(2) # Wait 2 seconds before retrying
                else:
                    print("      ❌ Failed to generate audio after max retries.")
                    raise e # Re-raise error if all retries fail

    def get_audio_duration(self, file_path):
        try:
            # mutagen.File detects the format, so this reads Gemini WAVs and edge MP3s alike.
            audio = mutagen.File(file_path)
            return audio.info.length
        except Exception as e:
            print(f"❌ Error reading audio length: {e}")
            return 0.0

    async def process_script(self, script_data):
        if self.provider == "gemini":
            return await asyncio.to_thread(self._narrate_gemini, script_data)

        print(f"🎙️ Starting Audio Generation for {len(script_data)} scenes (voice: {self.edge_voice})...")
        return await self._narrate_edge(script_data)

    async def _narrate_edge(self, script_data):
        """Voices every scene with edge-tts; failed scenes are dropped."""
        # Only scenes that actually got audio are kept: the composer reads
        # 'audio_path'/'duration' unconditionally and would crash on a scene
        # that was skipped here.
        narrated = []

        for scene in script_data:
            scene_id = scene['id']
            file_path = os.path.join(self.output_dir, f"voice_{scene_id}.mp3")

            try:
                await self._edge_tts(scene['text'], file_path)

                # Get Duration
                duration = self.get_audio_duration(file_path)
                if duration <= 0:
                    print(f"   ❌ Skipping Scene {scene_id}: unreadable audio duration.")
                    continue

                # Update Scene Data
                scene['audio_path'] = file_path
                scene['duration'] = duration
                narrated.append(scene)

                print(f"   ✅ Scene {scene_id}: {duration:.2f}s generated.")

                # CRITICAL: Sleep for 1 second to be polite to the API
                # This prevents the "Connection Timeout" error
                await asyncio.sleep(1)

            except Exception:
                print(f"   ❌ Skipping Scene {scene_id} due to audio error.")
                continue

        if len(narrated) < len(script_data):
            print(f"   ⚠️ {len(script_data) - len(narrated)} scene(s) dropped without audio.")

        return narrated
