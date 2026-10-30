import os
import json
import time
import hashlib
import requests
import random
from dotenv import load_dotenv

from modules.api_log import log_api_call

# Stock video source, chosen with VIDEO_PROVIDER in .env. Each provider needs its own key.
PROVIDER_KEYS = {
    "pexels": "PEXELS_API_KEY",
    "pixabay": "PIXABAY_API_KEY",
}
DEFAULT_PROVIDER = "pixabay"

# Pixabay requires API responses to be cached for 24 hours; Pexels gets the same
# cache so repeated runs don't burn quota.
SEARCH_CACHE_TTL = 24 * 60 * 60

# Pixabay rendition preference: 'medium' is ~1080p and present on every video;
# 'large' is usually 4K (slow to download and render) and is often missing.
PIXABAY_RENDITION_ORDER = ("medium", "large", "small", "tiny")

# Without a pick from the LLM, a clip is drawn from this many of the best search matches.
UNPICKED_POOL = 3
# Search terms lead with the subject, so a retry keeps this many of the first words.
RETRY_QUERY_WORDS = 2


def get_provider():
    """Returns the configured provider name, or raises if VIDEO_PROVIDER is unknown."""
    provider = (os.getenv("VIDEO_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
    if provider not in PROVIDER_KEYS:
        raise RuntimeError(f"VIDEO_PROVIDER='{provider}' is not supported. Use one of: {', '.join(PROVIDER_KEYS)}.")
    return provider


class AssetManager:
    def __init__(self):
        load_dotenv()
        self.provider = get_provider()
        key_name = PROVIDER_KEYS[self.provider]
        self.api_key = os.getenv(key_name)
        if not self.api_key:
            raise RuntimeError(f"{key_name} is not set (VIDEO_PROVIDER={self.provider}). Create a .env file or set the environment variable before running.")
        self.label = self.provider.capitalize()

        # Ensure download directory exists
        self.assets_dir = os.path.join(os.getcwd(), "assets", "video_clips")
        os.makedirs(self.assets_dir, exist_ok=True)
        self.cache_dir = os.path.join(os.getcwd(), "assets", "search_cache", self.provider)
        os.makedirs(self.cache_dir, exist_ok=True)

    def _log_limits(self, response):
        """Both providers report their limit in the response headers, for the panel's quota list."""
        try:
            limit, remaining, reset = (int(response.headers[f"X-RateLimit-{name}"])
                                       for name in ("Limit", "Remaining", "Reset"))
        except (KeyError, ValueError):
            return
        # Pixabay's reset is seconds from now, Pexels' a timestamp.
        log_api_call("Stock", self.label, limit=limit, remaining=remaining,
                     resets_at=reset if reset > 10 ** 9 else round(time.time()) + reset)

    # --- Provider APIs ---
    # Each returns a list of candidates: {"duration", "width", "height", "url", "tags"},
    # or None on API errors. "tags" describes what the clip shows, so the LLM can
    # judge whether it actually fits the narration.

    def _search_pexels(self, query):
        params = {
            "query": query,
            "per_page": 5,        # Fetch top 5 results to pick from
            "orientation": "portrait",
            "size": "medium"      # 'medium' is usually HD ready, saves bandwidth
        }
        response = requests.get("https://api.pexels.com/videos/search",
                                headers={"Authorization": self.api_key}, params=params, timeout=10)
        self._log_limits(response)
        if response.status_code != 200:
            print(f"      ⚠️ API Error: {response.status_code}")
            return None

        candidates = []
        for video in response.json().get("videos", []):
            # Get best quality video file link
            video_files = sorted(video.get("video_files", []), key=lambda x: (x.get("width") or 0) * (x.get("height") or 0), reverse=True)
            if video_files:
                best = video_files[0]
                # Pexels has no tags; its page URL slug is the clip's title
                # (".../video/woman-walking-on-the-beach-856971/").
                slug = video.get("url", "").rstrip("/").rsplit("/", 1)[-1]
                tags = " ".join(w for w in slug.split("-") if not w.isdigit())
                candidates.append({"duration": video.get("duration", 0), "width": best.get("width") or 0,
                                   "height": best.get("height") or 0, "url": best["link"], "tags": tags})
        return candidates

    def _search_pixabay(self, query):
        params = {
            "key": self.api_key,
            "q": query[:100],      # Pixabay rejects queries over 100 characters
            "lang": "en",
            "video_type": "film",  # Skip animations, keep real footage
            "safesearch": "true",
            "per_page": 20,        # Pixabay has no orientation filter, so fetch more to find portrait clips
        }
        response = requests.get("https://pixabay.com/api/videos/", params=params, timeout=10)
        self._log_limits(response)
        if response.status_code == 429:
            reset = response.headers.get("X-RateLimit-Reset", "?")
            print(f"      ⚠️ Pixabay rate limit hit. Resets in {reset}s.")
            return None
        if response.status_code != 200:
            print(f"      ⚠️ API Error: {response.status_code}")
            return None

        candidates = []
        for video in response.json().get("hits", []):
            renditions = video.get("videos", {})
            for name in PIXABAY_RENDITION_ORDER:
                rendition = renditions.get(name) or {}
                if rendition.get("url"):
                    candidates.append({"duration": video.get("duration", 0), "width": rendition.get("width", 0),
                                       "height": rendition.get("height", 0), "url": rendition["url"],
                                       "tags": video.get("tags", "")})
                    break
        return candidates

    def _search_api(self, query):
        """Runs the provider search, served from the 24h disk cache when fresh."""
        digest = hashlib.sha1(query.lower().encode("utf-8")).hexdigest()
        cache_path = os.path.join(self.cache_dir, f"{digest}.json")
        if os.path.exists(cache_path) and time.time() - os.path.getmtime(cache_path) < SEARCH_CACHE_TTL:
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, ValueError):
                pass  # Corrupt cache entry: fall through and refetch

        search = self._search_pixabay if self.provider == "pixabay" else self._search_pexels
        candidates = search(query)
        if candidates is not None:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(candidates, f)
        return candidates

    def find_candidates(self, query, duration_min=4):
        """
        Searches the configured provider for portrait videos matching the query.
        Returns the usable candidates ({"url", "tags", ...}), best search match first.
        """
        print(f"   🔍 Searching {self.label} for: '{query}'...")

        try:
            candidates = self._search_api(query)
            if candidates is None:
                return []

            if not candidates:
                # Retry strategy: Simplify query if complex query fails. Search terms
                # lead with the subject ("river flowing forest drone"), so the last
                # word is the one to lose, and the one least worth searching for alone.
                if " " in query:
                    simple_query = query.rsplit(" ", 1)[0]
                    print(f"      ⚠️ No results. Retrying with '{simple_query}'...")
                    return self.find_candidates(simple_query, duration_min)
                return []

            # Filter logic: Prefer portrait (9:16) clips; the composer crops landscape ones
            portrait = [c for c in candidates if c["height"] > c["width"]]
            pool = portrait or candidates

            # Prefer videos that aren't too short (at least 4 seconds)
            return [c for c in pool if c["duration"] >= duration_min] or pool

        except Exception as e:
            print(f"      ❌ Error searching {self.label}: {e}")
            return []

    def _pick_with_retry(self, searches, pick_clips):
        """
        Has pick_clips choose for every search. Where no clip fit, searches again for
        just the subject, once: a long query often only matches on its minor words.
        A clip already picked for an earlier search counts as not fitting: related
        searches return many of the same clips, and a video shows each only once.
        Returns {search index: clip index, or None if nothing fits}.
        """
        picks = pick_clips(searches)
        taken = set()
        for i, search in enumerate(searches):
            if picks.get(i) is not None:
                url = search["clips"][picks[i]]["url"]
                if url in taken:
                    picks[i] = None
                taken.add(url)

        retries = {}
        for i, search in enumerate(searches):
            subject = " ".join(search["query"].split()[:RETRY_QUERY_WORDS])
            if i in picks and picks[i] is None and subject != search["query"]:
                print(f"      ⚠️ Nothing fits '{search['query']}'. Retrying with '{subject}'...")
                fresh = [c for c in self.find_candidates(subject) if c["url"] not in taken]
                if fresh:
                    search.update(query=subject, clips=fresh)
                    retries[i] = search
        if retries:
            order = list(retries)
            for position, pick in pick_clips([retries[i] for i in order]).items():
                picks[order[position]] = pick
        return picks

    def _choose(self, search, picks, index, used):
        """
        The picked clip, or None if nothing fits or this video already used it.
        Without a pick, one of the best unused search matches, so videos don't all
        reuse the top hit. Adds the clip to used.
        """
        clips = search["clips"]
        if not clips or (index in picks and picks[index] is None):
            return None
        fresh = [c for c in clips if c["url"] not in used]
        if index in picks:
            clip = clips[picks[index]]
            if clip not in fresh:
                return None
        elif fresh:
            clip = random.choice(fresh[:UNPICKED_POOL])
        else:
            return None
        used.add(clip["url"])
        return clip

    def download_video(self, url, filename):
        """
        Downloads the video content to a local file.
        """
        # The URL's hash is part of the name, so a cached file is only reused for
        # the same clip — never for a different video's scene of the same number
        # left behind by a run that failed before cleanup.
        stem, ext = os.path.splitext(filename)
        url_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
        save_path = os.path.join(self.assets_dir, f"{stem}_{url_hash}{ext}")

        # Caching strategy
        if os.path.exists(save_path):
            return save_path

        try:
            with requests.get(url, stream=True, timeout=15) as r:
                r.raise_for_status()
                with open(save_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            return save_path
        except Exception as e:
            print(f"      ❌ Error downloading {filename}: {e}")
            return None

    def get_videos(self, script_data, pick_clips=None):
        """
        NEW LOGIC: Downloads TWO videos per scene (A and B).
        pick_clips (ContentBrain.pick_stock_clips) chooses among what each search
        found, given the line being spoken.
        Returns a list of tuples: [(path_a, path_b), (path_a, path_b), ...]
        Also records what was searched and found on each scene as
        scene['footage'] = [{"query", "found"}, ...] ("found" is None when nothing was).
        """
        print(f"🎥 Starting Double-Feature Video Download ({self.label})...")
        video_pairs = []

        # 1. Get Search Terms, and search them all so one request can pick every clip
        searches = []
        for scene in script_data:
            # Fallback to 'keywords' if visual_1/2 don't exist (compatibility mode)
            query_a = scene.get('visual_1', scene.get('keywords', 'abstract'))
            query_b = scene.get('visual_2', query_a) # Use A if B is missing
            for query in (query_a, query_b):
                searches.append({"line": scene.get('text', ''), "query": query,
                                 "clips": self.find_candidates(query)})
        picks = self._pick_with_retry(searches, pick_clips) if pick_clips else {}
        used = set()

        for number, scene in enumerate(script_data):
            scene_id = scene['id']
            search_a, search_b = searches[number * 2], searches[number * 2 + 1]
            query_a, query_b = search_a["query"], search_b["query"]

            clip_a = self._choose(search_a, picks, number * 2, used)
            clip_b = self._choose(search_b, picks, number * 2 + 1, used)
            # A scene without footage gets dropped, narration and all, unless the
            # avatar covers it. Rather show a poor match; rate_footage_fit marks it
            # as one, which sends the avatar here first.
            if not clip_a and not clip_b:
                clip_a = self._choose(search_a, {}, number * 2, used)
                clip_b = self._choose(search_b, {}, number * 2 + 1, used)
            # Or, if all they found is on screen already, a clip shown twice.
            if not clip_a and not clip_b:
                clip_a = self._choose(search_a, {}, number * 2, set()) or self._choose(search_b, {}, number * 2 + 1, set())

            # 2. Download Clip A
            path_a = None
            if clip_a:
                path_a = self.download_video(clip_a["url"], f"scene_{scene_id}_a.mp4")

            # 3. Download Clip B
            path_b = None
            if clip_b:
                path_b = self.download_video(clip_b["url"], f"scene_{scene_id}_b.mp4")

            scene['footage'] = [
                {"query": query_a, "found": (clip_a.get("tags") or "untitled clip") if path_a else None},
                {"query": query_b, "found": (clip_b.get("tags") or "untitled clip") if path_b else None},
            ]

            # 4. Fallback Logic (Self-Healing)
            # If one clip fails, the other carries the scene: the composer plays a
            # pair of the same clip as one shot.
            if not path_a and path_b:
                path_a = path_b
                print(f"      ⚠️ Scene {scene_id} Clip A missing. Using Clip B for both.")
            if not path_b and path_a:
                path_b = path_a
                print(f"      ⚠️ Scene {scene_id} Clip B missing. Using Clip A for both.")

            # 5. Final Check
            if path_a and path_b:
                video_pairs.append((path_a, path_b))
                print(f"   ✅ Scene {scene_id} Ready (A + B).")
            else:
                print(f"   ❌ Scene {scene_id} Completely Failed (No videos found).")
                video_pairs.append(None)

        return video_pairs

# --- TESTING ---
if __name__ == "__main__":
    manager = AssetManager()

    # Test with new dual-visual format
    test_script = [
        {
            "id": 1,
            "visual_1": "cyberpunk city neon",
            "visual_2": "hacker typing computer"
        }
    ]

    results = manager.get_videos(test_script)
    print("🎥 Assets Downloaded:", results)
