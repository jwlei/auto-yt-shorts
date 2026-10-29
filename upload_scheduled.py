#!/usr/bin/env python3
"""
Auto YT Shorts — Scheduled upload: 2 videos per day.
Uploads 2 videos, and schedules the next ones for the following day via cron.

Usage:
    python upload_scheduled.py              # Upload the next 2 videos
    python upload_scheduled.py --setup-cron  # Set up the automatic cron job
    python upload_scheduled.py --status      # Show progress
"""

import os
import sys
import json
import time
import argparse
import subprocess
from datetime import datetime

# Add the project directory to the path
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

from modules.uploader import authenticate, upload_video
from modules.metadata_generator import generate_metadata_batch, load_metadata
from modules.paths import OUTPUT_DIR, UPLOADED_DIR, move_to_uploaded

# Pending videos live in 0_output; each one moves to 1_uploaded once it is up.
VIDEO_DIR = OUTPUT_DIR
HISTORY_FILE = os.path.join(UPLOADED_DIR, "upload_history.json")
METADATA_FILE = os.path.join(VIDEO_DIR, "metadata_review.json")
SCHEDULE_LOG = os.path.join(PROJECT_DIR, "upload_schedule.log")
VIDEOS_PER_DAY = 2
DELAY_BETWEEN_UPLOADS = 120  # 2 minutes between each video


def log(msg):
    """Logs with a timestamp to the console and the log file."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    with open(SCHEDULE_LOG, 'a', encoding='utf-8') as f:
        f.write(line + "\n")


def load_history():
    """Loads the upload history."""
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return []
    return []


def save_history(history):
    """Saves the updated history."""
    os.makedirs(UPLOADED_DIR, exist_ok=True)
    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=4)


def get_pending_videos():
    """Returns the list of videos not yet uploaded."""
    if not os.path.exists(VIDEO_DIR):
        return []

    all_videos = sorted([f for f in os.listdir(VIDEO_DIR) if f.lower().endswith('.mp4')])
    history = load_history()
    uploaded = [
        entry.get('filename', entry) if isinstance(entry, dict) else entry
        for entry in history
    ]
    return [v for v in all_videos if v not in uploaded]


def ensure_metadata():
    """Makes sure metadata exists, generating it if needed."""
    if os.path.exists(METADATA_FILE):
        meta = load_metadata(METADATA_FILE)
        if meta:
            return meta

    log("🤖 Generating viral metadata with Gemini AI...")
    meta = generate_metadata_batch(VIDEO_DIR)
    return meta


def upload_batch(count=VIDEOS_PER_DAY):
    """Uploads the next N videos."""
    pending = get_pending_videos()

    if not pending:
        log("✅ All videos have already been uploaded! Nothing to do.")
        return 0

    batch = pending[:count]
    log(f"📤 Uploading {len(batch)} of {len(pending)} pending videos...")

    # Load/generate metadata
    metadata_list = ensure_metadata()

    # Authenticate
    log("🔐 Authenticating with YouTube...")
    youtube = authenticate()
    if not youtube:
        log("❌ Authentication failed!")
        return 1

    # Upload
    history = load_history()
    success = 0

    for i, video in enumerate(batch, 1):
        video_path = os.path.join(VIDEO_DIR, video)

        # Look up the video's metadata
        meta = None
        for m in metadata_list:
            if m.get('filename') == video:
                meta = m
                break

        if not meta:
            meta = {
                "title": f"🔥 {os.path.splitext(video)[0].replace('_', ' ')}",
                "description": "Follow for more! 🔔\n\n#Shorts #Viral #FYP #ForYou",
                "tags": ["shorts", "viral", "fyp", "foryou", "trending", "facts"]
            }

        log(f"\n{'═' * 50}")
        log(f"📤 Upload {i}/{len(batch)}: {video}")
        log(f"📝 Title: {meta.get('title')}")
        log(f"🏷️  Tags: {', '.join(meta.get('tags', [])[:8])}...")

        lang_code = meta.get('language') or 'en-GB'
        try:
            response = upload_video(
                youtube=youtube,
                video_path=video_path,
                title=meta.get('title'),
                description=meta.get('description'),
                tags=meta.get('tags', []),
                privacy='public',
                default_language=lang_code,
                default_audio_language=lang_code
            )

            if response:
                success += 1
                history_entry = {
                    "filename": video,
                    "video_id": response.get('id'),
                    "title": meta.get('title'),
                    "url": f"https://youtu.be/{response.get('id')}",
                    "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
                history.append(history_entry)
                save_history(history)
                log(f"🎉 Uploaded! https://youtu.be/{response.get('id')}")
                move_to_uploaded(video_path)

        except Exception as e:
            log(f"❌ Failed: {e}")

        # Delay between uploads
        if i < len(batch):
            log(f"⏳ Waiting {DELAY_BETWEEN_UPLOADS}s...")
            time.sleep(DELAY_BETWEEN_UPLOADS)

    # Summary
    remaining = len(pending) - success
    log(f"\n{'═' * 50}")
    log(f"📊 Result: {success}/{len(batch)} uploaded today")
    log(f"📦 Remaining: {remaining} videos")

    if remaining > 0:
        days_left = (remaining + VIDEOS_PER_DAY - 1) // VIDEOS_PER_DAY
        log(f"📅 Estimate: {days_left} more day(s) to finish")

    return 0 if success == len(batch) else 1


def show_status():
    """Shows overall progress."""
    all_videos = sorted([f for f in os.listdir(VIDEO_DIR) if f.lower().endswith('.mp4')]) if os.path.exists(VIDEO_DIR) else []
    history = load_history()
    pending = get_pending_videos()

    print()
    print("═" * 50)
    print("  📊 SCHEDULED UPLOAD STATUS")
    print("═" * 50)
    print(f"  Total videos:       {len(all_videos)}")
    print(f"  ✅ Uploaded:        {len(history)}")
    print(f"  ⏳ Pending:         {len(pending)}")
    print(f"  📅 Pace:            {VIDEOS_PER_DAY}/day")

    if pending:
        days_left = (len(pending) + VIDEOS_PER_DAY - 1) // VIDEOS_PER_DAY
        print(f"  🏁 Done in:         ~{days_left} day(s)")

    if history:
        print(f"\n  📋 Recently uploaded:")
        for entry in history[-4:]:
            if isinstance(entry, dict):
                print(f"     • {entry.get('title', 'N/A')}")
                print(f"       {entry.get('url', 'N/A')} ({entry.get('uploaded_at', '')})")

    if pending:
        print(f"\n  ⏭️  Next in queue:")
        for v in pending[:VIDEOS_PER_DAY]:
            print(f"     • {v}")

    print()
    print("═" * 50)


def setup_cron():
    """Sets up cron to upload 2 videos per day at 10am."""
    cron_cmd = f"0 10 * * * cd {PROJECT_DIR} && /usr/bin/python3 {PROJECT_DIR}/upload_scheduled.py >> {SCHEDULE_LOG} 2>&1"

    # Check whether it already exists
    result = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
    existing = result.stdout if result.returncode == 0 else ""

    if 'upload_scheduled.py' in existing:
        print("⚠️  Cron already set up! Existing entry:")
        for line in existing.split('\n'):
            if 'upload_scheduled' in line:
                print(f"   {line}")
        return

    # Add the new cron entry
    new_crontab = existing.rstrip() + "\n" + cron_cmd + "\n"
    process = subprocess.run(['crontab', '-'], input=new_crontab, capture_output=True, text=True)

    if process.returncode == 0:
        print("✅ Cron set up successfully!")
        print(f"   📅 Will run every day at 10:00")
        print(f"   📤 Will upload {VIDEOS_PER_DAY} videos per run")
        print(f"   📝 Log at: {SCHEDULE_LOG}")
    else:
        print(f"❌ Error setting up cron: {process.stderr}")


def main():
    parser = argparse.ArgumentParser(description="Scheduled YouTube Shorts upload (2/day)")
    parser.add_argument("--setup-cron", action="store_true", help="Set up the automatic cron job (every day at 10am)")
    parser.add_argument("--status", action="store_true", help="Show upload progress")
    parser.add_argument("--count", type=int, default=VIDEOS_PER_DAY, help=f"How many videos to upload now (default: {VIDEOS_PER_DAY})")

    args = parser.parse_args()

    if args.status:
        show_status()
        return 0

    if args.setup_cron:
        setup_cron()
        return 0

    log("\n🎬 Auto YT Shorts — Scheduled Upload")
    log(f"📅 Target: {args.count} videos per run\n")
    return upload_batch(args.count)


if __name__ == "__main__":
    sys.exit(main())
