#!/usr/bin/env python3
"""
Uploads finished videos from 0_output to YouTube and moves them to 1_uploaded.

Gemini writes each title, description and tags from the topic and narration
recorded in logs/generated_topics.jsonl when the video was made. Videos made
before that history existed need --topic, one at a time. The web UI's Upload
and Upload all buttons run this.

    python publish.py short_20261029_233100.mp4 --privacy unlisted
    python publish.py --all
"""
import argparse
import os
import sys

from dotenv import load_dotenv

from modules.paths import OUTPUT_DIR
from modules.topic_history import find_video_entry

load_dotenv()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Upload finished videos from 0_output to YouTube.")
    parser.add_argument("videos", nargs="*", help="File names in 0_output, e.g. short_20261029_233100.mp4")
    parser.add_argument("--all", action="store_true", help="Upload every video in 0_output.")
    parser.add_argument("--privacy", choices=("private", "unlisted", "public"), default="private",
                        help="Privacy status (default: private).")
    parser.add_argument("--topic", help="What the video is about, for a single video with no recorded topic.")
    args = parser.parse_args(argv)
    if args.all == bool(args.videos):
        parser.error("name the videos to upload, or pass --all")
    if args.topic and (args.all or len(args.videos) > 1):
        parser.error("--topic only works for a single video")
    return args


def main(argv=None):
    args = parse_args(argv)
    if args.all:
        names = sorted(n for n in os.listdir(OUTPUT_DIR) if n.lower().endswith(".mp4")) if os.path.isdir(OUTPUT_DIR) else []
        if not names:
            print("✅ Nothing in 0_output to upload.")
            return 0
    else:
        names = [os.path.basename(v) for v in args.videos]

    try:
        # Imported here so a missing Google client library is reported, not a crash.
        from modules.brain import ContentBrain
        from modules.publisher import publish_video
    except ImportError as e:
        print(f"❌ Upload Error: {e}. Run: pip install -r requirements.txt")
        return 1
    brain = ContentBrain()

    uploaded, failed = 0, []
    for number, name in enumerate(names, 1):
        print(f"\n📼 {number}/{len(names)}: {name}")
        path = os.path.join(OUTPUT_DIR, name)
        entry = find_video_entry(name) or {}
        topic = (args.topic or entry.get("topic") or "").strip()
        if not os.path.exists(path):
            print(f"❌ {name} is not in 0_output.")
        elif not topic:
            print(f"❌ No topic was recorded for {name}. Upload it on its own and say what it is about.")
        else:
            print(f"🎯 Topic: {topic}")
            try:
                _, url = publish_video(path, topic, entry.get("narration", ""), args.privacy, brain)
            except Exception as e:
                print(f"❌ Upload Error: {e}")
                url = None
            if url:
                uploaded += 1
                continue
        failed.append(name)

    print(f"\n📦 Uploaded {uploaded}/{len(names)} video(s).")
    for name in failed:
        print(f"   • not uploaded: {name}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
