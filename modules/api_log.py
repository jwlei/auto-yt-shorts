import json
import os
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from modules.paths import PROJECT_ROOT

LOG_PATH = os.path.join(PROJECT_ROOT, "logs", "api_errors.log")
USAGE_PATH = os.path.join(PROJECT_ROOT, "logs", "api_usage.jsonl")
# Models the server said are used up, and when each is back, for every process to see.
EXHAUSTED_PATH = os.path.join(PROJECT_ROOT, "logs", "exhausted_models.json")

# Google's daily quotas roll over at midnight Pacific, whatever the local time is.
PACIFIC = ZoneInfo("America/Los_Angeles")


def quota_day(moment=None):
    """The quota day of a datetime (a naive one is local time, as in the error log), by default now."""
    return (moment or datetime.now()).astimezone(PACIFIC).date()


def is_daily_quota(error):
    """True for an error about the per-day limit: retrying today is pointless. (The last two are YouTube's.)"""
    text = str(error).lower()
    return any(mark in text for mark in ("per day", "perday", "quotaexceeded", "dailylimitexceeded"))


def _wait_seconds(text):
    """'Please retry in 7h38m32.93s' / retryDelay '45s' -> how many seconds that is, or None."""
    match = re.search(r"retry in ((?:\d+h)?(?:\d+m)?(?:[\d.]+s?)?)", text, re.I) \
        or re.search(r"'retryDelay':\s*'([\d.]+s)'", text)
    if not match or not match.group(1):
        return None
    parts = {unit: float(value) for value, unit in re.findall(r"([\d.]+)([hms]?)", match.group(1))}
    return parts.get("h", 0) * 3600 + parts.get("m", 0) * 60 + parts.get("s", parts.get("", 0))


def _next_reset():
    tomorrow = datetime.now(PACIFIC).date() + timedelta(days=1)
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=PACIFIC).timestamp()


def _read_exhausted():
    try:
        with open(EXHAUSTED_PATH, "r", encoding="utf-8") as f:
            return {model: at for model, at in json.load(f).items() if isinstance(at, (int, float)) and at > time.time()}
    except (OSError, ValueError, AttributeError):
        return {}


def mark_exhausted(model, error):
    """The server refused this model for the day: skip it until the time it gave, or else the daily reset."""
    wait = _wait_seconds(str(error))
    exhausted = {**_read_exhausted(), model: time.time() + wait if wait else _next_reset()}
    temp = f"{EXHAUSTED_PATH}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(EXHAUSTED_PATH), exist_ok=True)
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(exhausted, f)
        os.replace(temp, EXHAUSTED_PATH)
    except OSError:
        pass  # Only costs one more refused request


def is_exhausted(model):
    return model in _read_exhausted()


def _short_wait(text):
    """'Please retry in 7h38m32.93s' / retryDelay '45s' -> '7h 38m' / '45s', or None."""
    total = _wait_seconds(text)
    if total is None:
        return None
    hours, minutes, seconds = int(total // 3600), int(total % 3600 // 60), int(total % 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m" if minutes else f"{seconds % 60}s"


def summarize_api_error(message):
    """
    One readable line for an API error: Google's 429s run to a paragraph of
    links and metric names, when all that matters is which limit and how long.
    """
    text = " ".join(str(message).split())
    lower = text.lower()
    wait = _short_wait(text)
    limit = re.search(r"limit:\s*(\d+)", text)
    if "429" in text or "resource_exhausted" in lower or "rate limit" in lower or "quota" in lower:
        if is_daily_quota(text):
            summary = "Daily limit reached" + (f" ({limit.group(1)}/day)" if limit else "")
            return summary + (f", resets in {wait}" if wait else "")
        summary = "Rate limited" + (f" ({limit.group(1)}/min)" if limit else "")
        return summary + (f", retry in {wait}" if wait else "")
    if "503" in text or "overloaded" in lower or "unavailable" in lower:
        return "Model overloaded, try again shortly"
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return first if len(first) <= 160 else first[:157] + "..."


def log_api_call(service, model, **limits):
    """Appends one successful call to logs/api_usage.jsonl, which the panel's quota list counts."""
    try:
        os.makedirs(os.path.dirname(USAGE_PATH), exist_ok=True)
        with open(USAGE_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": round(time.time()), "service": service, "model": model, **limits}) + "\n")
    except OSError:
        pass  # Logging must never break a run


def log_api_error(service, model, error):
    """
    Prints a one-line summary of an API error and appends the server's full
    message to logs/api_errors.log, so rate limits and quota problems are
    visible instead of looking like a hang.
    """
    status = getattr(error, "status_code", None) or getattr(error, "code", None) or ""
    message = " ".join(str(error).split())[:600]
    print(f"      🛑 {service} [{model}] {status}: {summarize_api_error(message)}")
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')}\t{service}\t{model}\t{status}\t{message}\n")
    except OSError:
        pass  # Logging must never break a run
