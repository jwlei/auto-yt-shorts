import os
import re
from datetime import datetime

LOG_PATH = os.path.join(os.getcwd(), "logs", "api_errors.log")


def is_daily_quota(error):
    """True for a 429 that is about the per-day limit: retrying today is pointless."""
    text = str(error).lower()
    return "per day" in text or "perday" in text


def _short_wait(text):
    """'Please retry in 7h38m32.93s' / retryDelay '45s' -> '7h 38m' / '45s', or None."""
    match = re.search(r"retry in ((?:\d+h)?(?:\d+m)?(?:[\d.]+s)?)", text, re.I) \
        or re.search(r"'retryDelay':\s*'([\d.]+s)'", text)
    if not match or not match.group(1):
        return None
    parts = {unit: value for value, unit in re.findall(r"([\d.]+)([hms])", match.group(1))}
    hours, minutes, seconds = (int(float(parts.get(u, 0))) for u in "hms")
    hours, minutes = hours + (minutes + seconds // 60) // 60, (minutes + seconds // 60) % 60
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
