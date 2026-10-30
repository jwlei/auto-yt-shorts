import os
import json
import re
import threading
import time
from google import genai
from dotenv import load_dotenv

from modules.api_log import is_daily_quota, log_api_error

load_dotenv()

# Tried in order; each has its own free-tier daily quota (20/day for the Flash
# models, 500/day for 3.5 Flash Lite), so a used-up model just hands over to the
# next. Override with GEMINI_MODEL (one model or a comma-separated chain).
DEFAULT_MODELS = ("gemini-3.8-flash", "gemini-3.5-flash", "gemini-3-flash-preview",
                  "gemini-2.5-flash", "gemini-3.5-flash-lite")

# Gemini returns these when it is overloaded or rate-limiting rather than when
# the request itself is wrong, so they are worth waiting out.
RETRYABLE_STATUSES = (429, 500, 502, 503, 504)

# Longest a single retry will sleep. A daily-quota 429 reports delays far past
# anything worth blocking a run on.
MAX_RETRY_WAIT = 90

# LTX follows the prompt over the audio: a held expression ("grim", "vacant")
# keeps her mouth shut while the narration plays. Goes around every speaking
# prompt, whatever the model wrote.
SPEAKING_LEAD = ("The presenter is talking to the camera the whole time: her lips and jaw move clearly "
                 "with every word, her mouth opening and closing as she speaks. ")
SPEAKING_TAIL = " She keeps talking throughout, mouth moving on every syllable."

def _get_client():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set. Create a .env file or set the environment variable before running.")
    return genai.Client(api_key=api_key)

# Small yes/no decisions (is this topic a repeat?) don't need the big models or
# their 20/day quota. Override with GEMINI_FAST_MODEL.
DEFAULT_FAST_MODELS = ("gemini-3.5-flash-lite", "gemini-2.5-flash-lite", "gemini-3.5-flash")

def _get_models(env_var="GEMINI_MODEL", defaults=DEFAULT_MODELS):
    # An unset CI variable arrives as an empty string, not as None.
    configured = os.getenv(env_var)
    if configured:
        return [m.strip() for m in configured.split(",") if m.strip()]
    return list(defaults)

def _is_retryable(error):
    status = getattr(error, "code", None) or getattr(error, "status_code", None)
    if status in RETRYABLE_STATUSES:
        return True
    # Not every client error exposes a numeric code, so fall back to the text.
    text = str(error)
    return any(str(code) in text for code in RETRYABLE_STATUSES)

def _retry_delay(error):
    """
    Reads the retryDelay Gemini attaches to a 429. Plain backoff is often
    shorter than what the server actually wants, which just burns the retries.
    """
    match = re.search(r"'retryDelay':\s*'(\d+(?:\.\d+)?)s'", str(error))
    if not match:
        return None
    # Cap it: a daily quota reports delays no run should sit and wait out.
    return min(float(match.group(1)) + 1, MAX_RETRY_WAIT)

# A text reply normally takes a few seconds. A model that says nothing for this
# long is overloaded or stuck (it happens, with no error at all), so move on.
DEFAULT_TIMEOUT = 90

# Models that timed out or used up their daily quota in this process: later calls
# skip them instead of waiting on them again.
_skipped_models = set()


def _call_with_deadline(fn, seconds):
    """
    Runs fn, raising TimeoutError if it has not returned within `seconds`. The
    SDK can sit on a silent request (or a long retry-after) indefinitely, so the
    call runs in a daemon thread that is simply abandoned if it hangs.
    """
    result = {}

    def target():
        try:
            result["value"] = fn()
        except BaseException as e:
            result["error"] = e

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise TimeoutError(f"no response within {seconds}s")
    if "error" in result:
        raise result["error"]
    return result["value"]


def _generate(prompt, retries=4, models=None):
    """
    Calls Gemini, waiting out the overload and per-minute rate limits that a
    scheduled run hits regularly. A model whose daily quota is used up, that
    doesn't answer within GEMINI_TIMEOUT, or that keeps failing hands over to the
    next model in the chain. Raises once every model has failed.
    """
    client = _get_client()
    timeout = int(os.getenv("GEMINI_TIMEOUT") or DEFAULT_TIMEOUT)
    errors = []

    for model in models or _get_models():
        if model in _skipped_models:
            continue
        for attempt in range(retries):
            try:
                return _call_with_deadline(
                    lambda: client.models.generate_content(model=model, contents=prompt), timeout)
            except Exception as e:
                log_api_error("Gemini", model, e)
                if isinstance(e, TimeoutError):
                    print(f"   ⚠️ {model} did not answer within {timeout}s. Trying the next model...")
                    _skipped_models.add(model)
                    break
                if is_daily_quota(e):
                    print(f"   ⚠️ {model} has used its daily quota. Trying the next model...")
                    _skipped_models.add(model)
                    break
                if attempt == retries - 1 or not _is_retryable(e):
                    break
                wait = _retry_delay(e) or 2 ** (attempt + 1)
                print(f"   ⚠️ {model} unavailable (attempt {attempt+1}/{retries}). Retrying in {wait:.0f}s...")
                time.sleep(wait)
        errors.append(model)

    raise RuntimeError(f"Gemini failed on every model ({', '.join(errors)}). See logs/api_errors.log.")

class ContentBrain:
    def get_trending_topic(self, excluded_topics=()):
        """
        In a full build, this would scrape Google Trends or Twitter.
        For now, we ask Gemini to pick a viral niche topic.
        """
        prompts = "Give me 1 specific, viral, and engaging topic for a Short Documentary. It should be a 'Engaging Did you know' fact or a 'Fun/intriguing Engaging News'. Return ONLY the topic name."
        excluded_topics = [topic.strip() for topic in excluded_topics if topic.strip()]
        if excluded_topics:
            prompts += "\nDo not repeat any of these previously used topics, including exact wording:\n"
            prompts += "\n".join(f"- {topic}" for topic in excluded_topics)
        response = _generate(prompts)
        topic = response.text.strip()
        print(f"🎯 Selected Topic: {topic}")
        return topic

    def find_repeat_topic(self, topic, used_topics):
        """
        Asks a fast model whether `topic` is the same video topic as one already
        made (a rewording or the same fact counts; a different fact in the same
        broad area does not). Returns the matching used topic, or None.
        Raises if no fast model answers, so the caller decides how to fail.
        """
        used_topics = [t.strip() for t in used_topics if t.strip()]
        if not used_topics:
            return None

        numbered = "\n".join(f"{n}. {t}" for n, t in enumerate(used_topics, 1))
        prompt = f"""
    A YouTube Shorts channel must not make two videos about the same topic.

    Candidate topic: {topic}

    Topics already made:
    {numbered}

    Is the candidate the SAME topic as one already made? Same topic means a viewer would
    feel they had already seen this video: the same fact, story, event or question, even
    if worded differently. A different fact within the same broad subject (e.g. another
    octopus fact) is NOT the same topic.

    Return ONLY strict JSON: {{"same": true, "match": <number of the matching topic>}}
    or {{"same": false, "match": null}}
    """
        response = _generate(prompt, retries=2, models=_get_models("GEMINI_FAST_MODEL", DEFAULT_FAST_MODELS))
        verdict = json.loads(response.text.replace('```json', '').replace('```', '').strip())
        if not isinstance(verdict, dict) or not isinstance(verdict.get("same"), bool):
            raise ValueError(f"unexpected repeat-check reply: {response.text[:200]}")
        if not verdict["same"]:
            return None
        match = verdict.get("match")
        if isinstance(match, int) and 1 <= match <= len(used_topics):
            return used_topics[match - 1]
        return "an earlier video"

    def generate_script(self, topic, lang="en-GB"):
        """
        Generates a structured JSON script with visual cues.
        """
        lang_instruction = "Strictly in BRITISH ENGLISH (UK spelling)" if lang == "en-GB" else "Strictly in AMERICAN ENGLISH (US spelling)"
        print(f"📝 Writing script in {lang} for: {topic}...")
        prompt = f"""
    You are the lead scriptwriter for a high-retention "Edutainment" YouTube Shorts channel.
    Topic: {topic}

    ### GOAL:
    Create a script where every sentence has a "Visual Switch". 
    To keep retention high, we need TWO different stock videos for every single scene.

    ### 1. SCRIPT REQUIREMENTS (The Voiceover):
    - **Language:** Write the text strictly in {lang_instruction}.
    - **Perspective:** Strictly **3rd Person** ("Scientists found...").
    - **Tone:** Engaging, fast-paced, logical. No fluff.
    - **Structure:** 8-9 Scenes total.
    - **Flow:** Hook -> Context -> Mechanism (How it works) -> Twist -> Outro.

    ### 2. VISUAL REQUIREMENTS (Dual Visuals):
    - For EVERY scene, provide TWO distinct search terms in **ENGLISH** (stock video search works best in English):
      - **visual_1:** Matches the *start* of the sentence.
      - **visual_2:** Matches the *end* of the sentence or provides a reaction/context.
    - **Strictly Literal:** If the text is "The economy crashed," do NOT search "sad man". Search "Stock market red chart".

    ### OUTPUT FORMAT (Strict JSON):
    [
        {{
            "id": 1,
            "text": "In 1995, fourteen wolves were released into Yellowstone Park, and they changed the rivers.",
            "visual_1": "wolves running snow aerial",
            "visual_2": "river flowing forest drone",
            "mood": "intriguing" 
        }},
        {{
            "id": 2,
            "text": "It sounds impossible, but the biology is actually simple math.",
            "visual_1": "person shocked looking at camera",
            "visual_2": "blackboard math equations chalk",
            "mood": "educational"
        }}
    ]
    """
    #     prompt = f"""
    # You are a master visual storyteller creating a viral YouTube Short.
    # Topic: {topic}
    
    # ### CRITICAL REQUIREMENTS:
    # 1. **Perspective:** Strictly **3rd Person** (e.g., "Scientists discovered..." or "The world changed..."). No "I" or "You".
    # 2. **Tone:** Cinematic, high-stakes, and slightly exaggerated.
    #    - Use **Power Words**: Instead of "big," use "colossal." Instead of "scary," use "terrifying."
    #    - The vibe should be "Mystery Documentary" (like Vox or National Geographic but faster).
    # 3. **Length:** Exactly **8 to 9 scenes**. Total read time 40-50 seconds.
    # 4. **Visual Strategy:** Keywords must be optimized for Stock Footage search.
    #    - Use simple, broad nouns: "storm clouds", "ancient ruins", "laboratory microscope".
    #    - Avoid complex actions or specific people.
    
    # ### STRUCTURE GUIDE:
    # - **Scene 1 (The Hook):** A mind-blowing statement or paradox. Grab attention immediately.
    # - **Scene 2-3 (The Mystery):** Establish why this is strange, dangerous, or important.
    # - **Scene 4-7 (The Climax):** The "Wait, what?" moment. The biggest twist or fact.
    # - **Scene 8-9 (The Mic Drop):** A final haunting thought or powerful conclusion.
    
    # ### OUTPUT FORMAT (Strict JSON):
    # [
    #     {{
    #         "id": 1,
    #         "text": "Deep beneath the Antarctic ice, something IMPOSSIBLE has just been detected.",
    #         "keywords": "glacier aerial drone cinematic",
    #         "mood": "ominous" 
    #     }},
    #     {{
    #         "id": 2,
    #         "text": "For centuries, maps showed this area as empty... they were wrong.",
    #         "keywords": "old map ancient paper table",
    #         "mood": "mystery"
    #     }}
    # ]
    # """
    

        # Unattended runs cannot recover from a single malformed reply, so retry.
        for attempt in range(3):
            response = _generate(prompt)

            # Clean the response to ensure it's valid JSON (sometimes AI adds markdown)
            clean_text = response.text.replace('```json', '').replace('```', '').strip()

            try:
                script_data = json.loads(clean_text)
            except json.JSONDecodeError:
                print(f"   ⚠️ Invalid JSON (attempt {attempt+1}/3). Raw output:")
                print(clean_text[:500])
                continue

            script_data = self._sanitize(script_data)
            if script_data:
                return script_data
            print(f"   ⚠️ Script had no usable scenes (attempt {attempt+1}/3).")

        print("❌ Script generation failed after 3 attempts.")
        return None

    def generate_metadata(self, topic, narration=""):
        """
        Writes the YouTube title, description and tags for a finished short.
        Falls back to the topic itself if the model misbehaves — a weak title is
        better than a failed upload.
        """
        prompt = f"""
    Write YouTube Shorts metadata for this video.

    Topic: {topic}
    Narration: {narration or "(not available: write from the topic)"}

    Rules:
    - "title": under 80 characters, curiosity-driven, no clickbait punctuation spam.
    - "description": 2-3 sentences summarizing the video, then a blank line, then "#Shorts".
    - "tags": 8-12 lowercase search keywords, no "#" prefix.

    Return ONLY strict JSON:
    {{"title": "...", "description": "...", "tags": ["...", "..."]}}
    """

        fallback = {
            "title": topic[:80],
            "description": f"{topic}\n\n#Shorts",
            "tags": [],
        }

        try:
            response = _generate(prompt)
            clean_text = response.text.replace('```json', '').replace('```', '').strip()
            metadata = json.loads(clean_text)
        except Exception as e:
            print(f"   ⚠️ Metadata generation failed ({e}). Using the topic as the title.")
            return fallback

        if not isinstance(metadata, dict) or not str(metadata.get('title', '')).strip():
            print("   ⚠️ Metadata had no usable title. Using the topic as the title.")
            return fallback

        tags = metadata.get('tags') or []
        if not isinstance(tags, list):
            tags = []

        description = str(metadata.get('description', '')).strip() or fallback['description']
        if "#Shorts" not in description:
            description = f"{description}\n\n#Shorts"

        return {
            "title": str(metadata['title']).strip(),
            "description": description,
            "tags": [str(tag).strip() for tag in tags if str(tag).strip()],
        }

    def write_avatar_prompt(self, scene, seconds, speaking=False):
        """
        Writes a video-model prompt for the presenter clip shown during a scene.
        speaking=True: the presenter says the line (lip-synced to the narration).
        speaking=False: the narration plays over her, so she reacts instead.
        Describes action only: the persona's look comes from the reference image
        or AVATAR_PERSONA.
        """
        mood = scene.get('mood', 'engaging')
        if speaking:
            fallback = (f"The presenter looks into the camera and says in a clear British accent: \"{scene['text']}\" "
                        f"Her expression is {mood}, with natural head movement and small hand gestures. "
                        "Static medium shot. Quiet room tone.")
            delivery = (f"- The presenter speaks this exact line to camera in a clear British accent: \"{scene['text']}\"\n"
                        "    - Quote the line word for word, then describe her expression, gestures and head movement while she says it.\n"
                        "    - Her mouth moves the whole time. Show the mood with her eyebrows, eyes and head; never a still, frozen, "
                        "vacant, unblinking or held expression, and never closed lips.")
        else:
            fallback = (f"0-{seconds} seconds: The presenter looks into the camera with a {mood} expression, "
                        "nods slowly and gestures with one open hand, mouth closed, listening. "
                        "Static medium shot, subtle handheld movement. Soft ambient room sound, no speech.")
            delivery = ("- The presenter does NOT speak: mouth closed, reacting to the line with expression and gestures.\n"
                        f"    - Write a short timeline of actions, e.g. \"0-4 seconds: ...\", \"4-{seconds} seconds: ...\".")

        prompt = f"""
    Write a prompt for an AI video model (LTX-2) for a {seconds}-second vertical clip of a
    YouTube Shorts presenter. The line for this moment is: "{scene['text']}" (mood: {mood}).

    Rules:
    {delivery}
    - Include one simple camera move. No music.
    - Do NOT describe the presenter's appearance, clothes or room.
    - Under 100 words. Return ONLY the prompt text.
    """

        try:
            text = _generate(prompt).text.strip()
        except Exception as e:
            print(f"   ⚠️ Avatar prompt failed ({e}). Using a generic reaction.")
            text = ""
        text = text or fallback
        return f"{SPEAKING_LEAD}{text}{SPEAKING_TAIL}" if speaking else text

    def rate_footage_fit(self, script_data):
        """
        Asks the LLM how well the stock footage found for each scene matches what
        the narration says. Returns {scene index: 0-10}; scenes where nothing was
        found are 0. If the model misbehaves every scene gets a neutral 5, so the
        avatar plan falls back to plain random picks.
        """
        fits, lines = {}, []
        for i, scene in enumerate(script_data):
            footage = scene.get('footage') or []
            found = [f for f in footage if f.get('found')]
            if not found:
                fits[i] = 0
                continue
            clips = "; ".join(f'searched "{f["query"]}" -> got "{f["found"]}"' for f in found)
            lines.append(f'{scene["id"]}. "{scene["text"]}" | {clips}')

        if lines:
            prompt = f"""
    You are the editor of a YouTube Short. For each scene below you get the narration line and
    the stock clips that were found for it (what was searched, and the found clip's tags/title).

    Rate 0-10 how well the found footage illustrates THIS line for a viewer:
    10 = shows exactly what is being said; 5 = loosely related / generic; 0 = unrelated or misleading.
    Judge the found clip, not the search term. Be strict: generic filler footage is a 3-4.

    Scenes:
    {chr(10).join(lines)}

    Return ONLY strict JSON: [{{"id": 1, "fit": 7}}, ...]
    """
            try:
                rated = json.loads(_generate(prompt).text.replace('```json', '').replace('```', '').strip())
                by_id = {int(r["id"]): max(0, min(10, int(r["fit"]))) for r in rated if isinstance(r, dict)}
                for i, scene in enumerate(script_data):
                    if i not in fits:
                        fits[i] = by_id.get(scene["id"], 5)
            except Exception as e:
                print(f"   ⚠️ Footage rating failed ({e}). Treating all footage as an average fit.")
                for i in range(len(script_data)):
                    fits.setdefault(i, 5)

        summary = ", ".join(f"{script_data[i]['id']}: {fits[i]}" for i in sorted(fits))
        print(f"🧠 Stock footage fit (scene: 0-10): {summary}")
        return fits

    @staticmethod
    def _sanitize(script_data):
        """
        Drops malformed scenes and renumbers ids so downstream modules can rely
        on 'id' and 'text' always being present.
        """
        if not isinstance(script_data, list):
            print(f"   ⚠️ Expected a list of scenes, got {type(script_data).__name__}.")
            return None

        scenes = []
        for raw in script_data:
            if not isinstance(raw, dict):
                continue
            text = str(raw.get('text', '')).strip()
            if not text:
                continue
            scene = dict(raw)
            scene['text'] = text
            scene['id'] = len(scenes) + 1
            scenes.append(scene)

        return scenes or None
        
# --- TESTING THE MODULE ---
if __name__ == "__main__":
    brain = ContentBrain()
    topic = brain.get_trending_topic()
    script = brain.generate_script(topic)
    
    # Save to file to verify
    with open("script.json", "w") as f:
        json.dump(script, f, indent=4)
        print("✅ Script saved to script.json")