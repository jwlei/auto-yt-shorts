import os
import shutil

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Finished videos land here; once uploaded they are moved to UPLOADED_DIR, so
# OUTPUT_DIR only ever holds videos still waiting to go up.
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "0_output")
UPLOADED_DIR = os.path.join(PROJECT_ROOT, "1_uploaded")


def move_to_uploaded(video_path):
    """Moves an uploaded video into UPLOADED_DIR. Returns the new path (or the old one if the move failed)."""
    os.makedirs(UPLOADED_DIR, exist_ok=True)
    target = os.path.join(UPLOADED_DIR, os.path.basename(video_path))
    try:
        shutil.move(video_path, target)
        print(f"   📁 Moved to {os.path.relpath(target, PROJECT_ROOT)}")
        return target
    except OSError as e:
        # The upload itself succeeded; a file left behind is only untidy.
        print(f"   ⚠️ Uploaded, but could not move {os.path.basename(video_path)}: {e}")
        return video_path
