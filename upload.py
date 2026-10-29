#!/usr/bin/env python3
"""
Auto YT Shorts — Batch upload to YouTube.
Uploads videos from the 0_output/ folder with AI-generated metadata.

Usage:
    python upload.py --generate-meta --privacy unlisted
    python upload.py --meta-file 0_output/metadata_review.json
    python upload.py --dry-run --generate-meta
"""

import os
import sys
import json
import time
import argparse

from modules.uploader import authenticate, upload_video
from modules.metadata_generator import generate_metadata_batch, load_metadata
from modules.paths import OUTPUT_DIR, UPLOADED_DIR, move_to_uploaded


# File that tracks already-uploaded videos (prevents duplicates)
HISTORY_FILENAME = "upload_history.json"


def _load_history(history_file):
    """Loads the history of previous uploads."""
    if os.path.exists(history_file):
        try:
            with open(history_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return []
    return []


def _save_history(history_file, history):
    """Saves the updated upload history."""
    os.makedirs(os.path.dirname(history_file), exist_ok=True)
    with open(history_file, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=4)


def _find_videos(video_dir):
    """Finds all .mp4 files in the directory."""
    if not os.path.exists(video_dir):
        return []
    return sorted([f for f in os.listdir(video_dir) if f.lower().endswith('.mp4')])


def _get_default_metadata(filename):
    """Builds default metadata when none is provided."""
    name = os.path.splitext(filename)[0].replace("_", " ").replace("-", " ")
    return {
        "filename": filename,
        "title": f"✨ {name}",
        "description": f"Watch our new Short!\n\n#Shorts #facts #trivia",
        "tags": ["shorts", "facts", "trivia", "education"]
    }


def _find_meta_for_video(metadata_list, filename):
    """Finds the metadata matching a video file."""
    for meta in metadata_list:
        if meta.get('filename') == filename:
            return meta
    return None


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Auto YT Shorts — Batch YouTube Shorts upload.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Generate metadata with AI and upload
  python upload.py --generate-meta

  # Preview what would be uploaded (without uploading)
  python upload.py --dry-run --generate-meta

  # Use metadata from a manually edited JSON file
  python upload.py --meta-file 0_output/metadata_review.json

  # Upload as unlisted with a shorter delay
  python upload.py --generate-meta --privacy unlisted --delay 60

  # Only generate metadata (no upload)
  python upload.py --generate-meta --meta-only
        """
    )

    parser.add_argument(
        "--dir", default=OUTPUT_DIR,
        help="Directory containing the .mp4 videos (default: 0_output/). Uploaded videos are moved to 1_uploaded/."
    )
    parser.add_argument(
        "--privacy", default="public",
        choices=["public", "unlisted", "private"],
        help="Video privacy level (default: public)"
    )
    parser.add_argument(
        "--generate-meta", action="store_true",
        help="Generate title, description and tags automatically with Gemini AI"
    )
    parser.add_argument(
        "--meta-file",
        help="Path to a JSON file with pre-generated metadata"
    )
    parser.add_argument(
        "--meta-only", action="store_true",
        help="Only generate metadata (no upload)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be uploaded without actually uploading"
    )
    parser.add_argument(
        "--delay", type=int, default=300,
        help="Seconds between uploads to avoid rate limits (default: 300)"
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip confirmation and upload directly"
    )
    parser.add_argument(
        "--reset-history", action="store_true",
        help="Clear the upload history (allows re-uploading videos)"
    )

    return parser.parse_args(argv)


def main():
    args = parse_args()

    print()
    print("═" * 60)
    print("  🎬 Auto YT Shorts — YouTube Shorts Uploader")
    print("═" * 60)
    print()

    video_dir = args.dir

    # 1. Check the video directory
    if not os.path.exists(video_dir):
        print(f"❌ Directory not found: {video_dir}")
        print(f"   💡 Create the folder and put your .mp4 videos in it.")
        return 1

    all_videos = _find_videos(video_dir)
    if not all_videos:
        print(f"⚠️  No .mp4 videos found in {video_dir}")
        return 1

    print(f"🔍 Found {len(all_videos)} videos in '{video_dir}':")
    for v in all_videos:
        size_mb = os.path.getsize(os.path.join(video_dir, v)) / (1024 * 1024)
        print(f"   • {v} ({size_mb:.1f} MB)")
    print()

    # 2. Manage upload history
    history_file = os.path.join(UPLOADED_DIR, HISTORY_FILENAME)

    if args.reset_history:
        if os.path.exists(history_file):
            os.remove(history_file)
            print("🗑️  Upload history cleared!")
        else:
            print("ℹ️  No history to clear.")

    history = _load_history(history_file)
    uploaded_files = [entry.get('filename', entry) if isinstance(entry, dict) else entry
                      for entry in history]

    videos_to_process = [v for v in all_videos if v not in uploaded_files]

    if not videos_to_process:
        print("✅ All videos have already been uploaded!")
        print(f"   💡 Use --reset-history to clear the history and re-upload.")
        return 0

    if len(videos_to_process) < len(all_videos):
        skipped = len(all_videos) - len(videos_to_process)
        print(f"⏭️  Skipping {skipped} previously uploaded video(s).")
        print()

    # 3. Generate or load metadata
    metadata_list = []

    if args.meta_file:
        metadata_list = load_metadata(args.meta_file)
        if not metadata_list:
            print("❌ No metadata loaded. Check the file.")
            return 1
    elif args.generate_meta:
        print("🤖 Generating metadata with Gemini AI...\n")
        metadata_list = generate_metadata_batch(video_dir)
        if not metadata_list:
            print("❌ Failed to generate metadata.")
            return 1

        if args.meta_only:
            print("✅ Metadata generated! File saved for review.")
            print(f"   📋 Edit the file, then run:")
            print(f"   python upload.py --meta-file {os.path.join(video_dir, 'metadata_review.json')}")
            return 0
    else:
        # Default metadata
        print("ℹ️  Using default metadata (use --generate-meta for AI).")
        metadata_list = [_get_default_metadata(v) for v in videos_to_process]

    # 4. Show summary
    print()
    print("─" * 60)
    print("  📋 UPLOAD SUMMARY")
    print("─" * 60)
    print(f"  Videos to upload: {len(videos_to_process)}")
    print(f"  Privacy:          {args.privacy}")
    print(f"  Delay between:    {args.delay}s")
    print()

    for v in videos_to_process:
        meta = _find_meta_for_video(metadata_list, v) or _get_default_metadata(v)
        print(f"  🎬 {v}")
        print(f"     📝 {meta.get('title', 'Untitled')}")
        tags_preview = ', '.join(meta.get('tags', [])[:5])
        if len(meta.get('tags', [])) > 5:
            tags_preview += "..."
        print(f"     🏷️  {tags_preview}")
        print()

    print("─" * 60)

    # 5. Dry-run mode
    if args.dry_run:
        print("\n🧪 DRY RUN — No videos were uploaded.")
        print("   Remove --dry-run to upload for real.")
        return 0

    # 6. Authenticate with YouTube
    print("\n🔐 Authenticating with the YouTube API...")
    youtube = authenticate()
    if not youtube:
        print("❌ Authentication failed. Exiting.")
        return 1

    # 7. Confirmation
    if not args.yes:
        print()
        confirm = input("🤔 Proceed with the upload? (y/N): ").strip().lower()
        if confirm not in ('y', 'yes'):
            print("🛑 Cancelled by user.")
            return 0

    # 8. Batch upload
    print(f"\n🚀 Starting upload of {len(videos_to_process)} video(s)...\n")

    success_count = 0
    failed = []

    for i, video in enumerate(videos_to_process, 1):
        video_path = os.path.join(video_dir, video)
        meta = _find_meta_for_video(metadata_list, video) or _get_default_metadata(video)

        print(f"{'═' * 60}")
        print(f"  📤 UPLOAD {i}/{len(videos_to_process)}: {video}")
        print(f"{'═' * 60}")

        lang_code = meta.get('language') or 'en-GB'
        try:
            response = upload_video(
                youtube=youtube,
                video_path=video_path,
                title=meta.get('title'),
                description=meta.get('description'),
                tags=meta.get('tags', []),
                privacy=args.privacy,
                default_language=lang_code,
                default_audio_language=lang_code
            )

            if response:
                success_count += 1

                # Save to history immediately (in case of a crash)
                history_entry = {
                    "filename": video,
                    "video_id": response.get('id'),
                    "title": meta.get('title'),
                    "url": f"https://youtu.be/{response.get('id')}",
                    "uploaded_at": time.strftime("%Y-%m-%d %H:%M:%S")
                }
                history.append(history_entry)
                _save_history(history_file, history)
                move_to_uploaded(video_path)
            else:
                failed.append(video)

        except Exception as e:
            print(f"   ❌ Failed to upload {video}: {e}")
            failed.append(video)

        # Delay between uploads (except after the last one)
        if i < len(videos_to_process):
            print(f"\n⏳ Waiting {args.delay} seconds before the next upload...")
            print(f"   (Ctrl+C to cancel)\n")
            try:
                time.sleep(args.delay)
            except KeyboardInterrupt:
                print("\n\n🛑 Upload interrupted by user.")
                break

    # 9. Final summary
    print()
    print("═" * 60)
    print("  📊 FINAL SUMMARY")
    print("═" * 60)
    print(f"  ✅ Uploaded successfully: {success_count}/{len(videos_to_process)}")

    if failed:
        print(f"  ❌ Failed: {len(failed)}")
        for f_name in failed:
            print(f"     • {f_name}")

    # Show links to the uploaded videos
    recent = [e for e in history if isinstance(e, dict) and e.get('url')]
    if recent:
        print()
        print("  🔗 Uploaded video links:")
        for entry in recent[-len(videos_to_process):]:
            if isinstance(entry, dict):
                print(f"     • {entry.get('title', 'N/A')}")
                print(f"       {entry.get('url', 'N/A')}")

    print()
    print("═" * 60)

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
