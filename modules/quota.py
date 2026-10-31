import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from modules.api_log import LOG_PATH, USAGE_PATH, is_daily_quota
from modules.asset_manager import get_provider
from modules.gemini_models import get_fast_models, get_image_models, get_text_models, get_tts_models

# Google's daily quotas roll over at midnight Pacific, whatever the local time is.
PACIFIC = ZoneInfo("America/Los_Angeles")

# Free-tier requests per day, until a real 429 reports the model's actual limit.
FREE_TIER_LIMITS = {
    "gemini-3.8-flash": 20, "gemini-3.5-flash": 20, "gemini-3-flash-preview": 20,
    "gemini-3.5-flash-lite": 500,
    "gemini-3.8-flash-tts": 10, "gemini-3.8-flash-lite-tts": 10,
}

YOUTUBE_UPLOADS = "uploads"
YOUTUBE_UPLOAD_COST = 1600
YOUTUBE_THUMBNAILS = "thumbnails"
YOUTUBE_THUMBNAIL_COST = 50
YOUTUBE_DAILY_UNITS = 10000

# The window each stock provider's limit headers describe.
STOCK_PERIODS = {"pixabay": "per min", "pexels": "per month"}


def _read_usage(today):
    """Successful calls per model on this quota day, and each model's latest reported limits."""
    calls, reported = Counter(), {}
    try:
        with open(USAGE_PATH, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    if datetime.fromtimestamp(entry["t"], PACIFIC).date() == today:
                        calls[entry["model"]] += 1
                    if "limit" in entry:
                        reported[entry["model"]] = entry
                except (ValueError, KeyError, TypeError):
                    continue  # A broken line only costs one call in the count
    except OSError:
        pass
    return calls, reported


def _read_limit_errors(today):
    """From the error log: the limit each daily 429 stated, and the models refused on this quota day."""
    limits, refused = {}, set()
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 5 or not is_daily_quota(parts[4]):
                    continue
                try:
                    if datetime.fromisoformat(parts[0]).astimezone(PACIFIC).date() == today:
                        refused.add(parts[2])
                except ValueError:
                    continue
                limit = re.search(r"limit:\s*(\d+)", parts[4])
                if limit:
                    limits[parts[2]] = int(limit.group(1))
    except OSError:
        pass
    return limits, refused


def _row(name, used, limit, out=False, period=None):
    if out:
        # The server's refusal beats our count: other apps on the key, or calls before tracking began.
        used = max(used, limit or 0)
    elif limit is not None and used > limit:
        limit = None  # More got through than the limit we had, so that limit is wrong (a paid tier, say)
    return {"name": name, "used": used, "limit": limit, "out": out, "period": period}


def _stock_row(reported, now):
    provider = get_provider()
    name = provider.capitalize()
    entry = reported.get(name)
    if not entry:
        return _row(name, 0, None, period=STOCK_PERIODS[provider])
    used = entry["limit"] - entry["remaining"] if now < entry["resets_at"] else 0
    return _row(name, used, entry["limit"], out=used >= entry["limit"], period=STOCK_PERIODS[provider])


def quota_summary():
    """
    What each API has used of its limit, for the panel. Google offers no way to
    ask what is left of a quota, so Gemini and YouTube are counted from this
    app's own successful calls, and a daily 429 pins a model to full. Stock
    providers report their own numbers in every response.
    """
    now = datetime.now(PACIFIC)
    today = now.date()
    calls, reported = _read_usage(today)
    limits, refused = _read_limit_errors(today)

    def gemini_row(model):
        return _row(model, calls[model], limits.get(model, FREE_TIER_LIMITS.get(model)), model in refused)

    midnight = datetime.combine(today + timedelta(days=1), datetime.min.time(), PACIFIC)
    # Through timestamps: subtracting two datetimes in one zone ignores a clock change.
    resets_in = round(midnight.timestamp() - now.timestamp())

    text_models = dict.fromkeys(get_text_models() + get_fast_models())
    return {
        "resets_in": resets_in,
        "groups": [
            {"title": "Gemini text", "rows": [gemini_row(m) for m in text_models]},
            {"title": "Gemini voice", "rows": [gemini_row(m) for m in get_tts_models()]},
            {"title": "Gemini images", "rows": [gemini_row(m) for m in get_image_models()]},
            {"title": "Stock footage", "rows": [_stock_row(reported, time.time())]},
            {"title": "YouTube", "rows": [_row("Upload units",
                                               calls[YOUTUBE_UPLOADS] * YOUTUBE_UPLOAD_COST
                                               + calls[YOUTUBE_THUMBNAILS] * YOUTUBE_THUMBNAIL_COST,
                                               YOUTUBE_DAILY_UNITS, YOUTUBE_UPLOADS in refused)]},
        ],
    }
