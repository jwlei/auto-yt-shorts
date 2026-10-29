import json
import os
import re
from datetime import datetime

from modules.paths import PROJECT_ROOT

TOPIC_HISTORY_PATH = os.path.join(PROJECT_ROOT, "logs", "generated_topics.jsonl")


def _topic_key(topic):
    return re.sub(r"\s+", " ", topic).strip().casefold()


def read_history():
    """Read the entries for successfully rendered videos, oldest first."""
    try:
        with open(TOPIC_HISTORY_PATH, "r", encoding="utf-8") as history:
            entries = []
            for line_number, line in enumerate(history, start=1):
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as e:
                    raise ValueError(f"Invalid topic history at line {line_number}: {e}") from e
                topic = entry.get("topic") if isinstance(entry, dict) else None
                if not isinstance(topic, str) or not topic.strip():
                    raise ValueError(f"Invalid topic history entry at line {line_number}: missing topic.")
                entries.append(entry)
            return entries
    except FileNotFoundError:
        return []


def read_successful_topics():
    """Read topics associated with successfully rendered videos."""
    return [entry["topic"] for entry in read_history()]


def find_video_entry(video_name):
    """The history entry for a video file name (its latest, if made twice), or None."""
    return next((entry for entry in reversed(read_history()) if entry.get("video") == video_name), None)


def find_exact_repeat(topic, used_topics):
    """Returns the used topic that matches ignoring case and spacing, or None."""
    key = _topic_key(topic)
    return next((used for used in used_topics if key and _topic_key(used) == key), None)


def record_successful_topic(topic, video_path, narration=""):
    """
    Append a topic only after its video has been successfully created. The
    narration lets a later upload write the title and description from it.
    """
    if not topic or not topic.strip():
        raise ValueError("Cannot record an empty topic.")
    os.makedirs(os.path.dirname(TOPIC_HISTORY_PATH), exist_ok=True)
    entry = {
        "topic": topic.strip(),
        "video": os.path.basename(video_path),
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if narration:
        entry["narration"] = narration
    with open(TOPIC_HISTORY_PATH, "a", encoding="utf-8") as history:
        history.write(json.dumps(entry, ensure_ascii=False) + "\n")
