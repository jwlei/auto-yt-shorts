import os
from datetime import datetime

LOG_PATH = os.path.join(os.getcwd(), "logs", "api_errors.log")


def is_daily_quota(error):
    """True for a 429 that is about the per-day limit: retrying today is pointless."""
    text = str(error).lower()
    return "per day" in text or "perday" in text


def log_api_error(service, model, error):
    """
    Prints an API error with the server's own message and appends it to
    logs/api_errors.log, so rate limits and quota problems are visible instead
    of looking like a hang.
    """
    status = getattr(error, "status_code", None) or getattr(error, "code", None) or ""
    message = " ".join(str(error).split())[:600]
    print(f"      🛑 {service} API error [{model}] {status}: {message}")
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')}\t{service}\t{model}\t{status}\t{message}\n")
    except OSError:
        pass  # Logging must never break a run
