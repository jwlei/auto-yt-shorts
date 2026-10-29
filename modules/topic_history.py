import json
import os
import re
from datetime import datetime

from modules.paths import PROJECT_ROOT

TOPIC_HISTORY_PATH = os.path.join(PROJECT_ROOT, "logs", "generated_topics.jsonl")


def _topic_key(topic):
    return re.sub(r"\s+", " ", topic).strip().casefold()


def read_successful_topics():
    """Read topics associated with successfully rendered videos."""
    try:
        with open(TOPIC_HISTORY_PATH, "r", encoding="utf-8") as history:
            topics = []
            for line_number, line in enumerate(history, start=1):
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as e:
                    raise ValueError(f"Invalid topic history at line {line_number}: {e}") from e
                topic = entry.get("topic") if isinstance(entry, dict) else None
                if not isinstance(topic, str) or not topic.strip():
                    raise ValueError(f"Invalid topic history entry at line {line_number}: missing topic.")
                topics.append(topic)
            return topics
    except FileNotFoundError:
        return []


def find_exact_repeat(topic, used_topics):
    """Returns the used topic that matches ignoring case and spacing, or None."""
    key = _topic_key(topic)
    return next((used for used in used_topics if key and _topic_key(used) == key), None)


def record_successful_topic(topic, video_path):
    """Append a topic only after its video has been successfully created."""
    if not topic or not topic.strip():
        raise ValueError("Cannot record an empty topic.")
    os.makedirs(os.path.dirname(TOPIC_HISTORY_PATH), exist_ok=True)
    entry = {
        "topic": topic.strip(),
        "video": os.path.basename(video_path),
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    with open(TOPIC_HISTORY_PATH, "a", encoding="utf-8") as history:
        history.write(json.dumps(entry, ensure_ascii=False) + "\n")
