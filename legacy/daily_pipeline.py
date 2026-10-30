#!/usr/bin/env python3
"""
AutoShorts AI — Fully Automated Daily Pipeline.
1. Generate new videos with AI (AI picks trending topics and writes the script, narration and edit)
2. Create viral metadata (title, description and 20 viral tags with Gemini)
3. Upload automatically to YouTube Shorts

Usage:
    python legacy/daily_pipeline.py --count 2          # Generate and publish 2 videos now
    python legacy/daily_pipeline.py --generate-only    # Only generate, don't upload
    python legacy/daily_pipeline.py --upload-only      # Only upload pending videos
"""

import os
import sys
import argparse
import asyncio
import subprocess
from datetime import datetime

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from main import main_async, parse_args as parse_main_args
from upload_scheduled import upload_batch, show_status, log


async def run_daily_pipeline(count=2, generate_only=False, upload_only=False, lang="en-GB"):
    log("\n" + "═" * 60)
    log(f"🚀 STARTING AUTONOMOUS DAILY PIPELINE [{lang}] — AUTOSHORTS AI")
    log("═" * 60)

    # 1. AI VIDEO GENERATION
    if not upload_only:
        log(f"\n🎬 [STEP 1/3] Generating {count} new video(s) [{lang}] with AI...")
        log("   • AI picking trending topics")
        log("   • Writing edutainment script (Gemini 3.5 Flash)")
        log(f"   • Generating {lang} narration (Edge-TTS)")
        log("   • Collecting HD videos (Pixabay/Pexels)")
        log("   • Rendering final video with transitions and avatar (FFmpeg)...")

        main_args = parse_main_args(["--runs", str(count), "--lang", lang])
        try:
            exit_code = await main_async(main_args)
            if exit_code == 0:
                log("✅ [STEP 1/3] Video generation completed successfully!")
            else:
                log("⚠️ [STEP 1/3] Some videos may not have been fully generated.")
        except Exception as e:
            log(f"❌ [STEP 1/3] Error during video generation: {e}")
            if generate_only:
                return 1

    if generate_only:
        log("✨ Generation step finished (--generate-only set).")
        return 0

    # 2. UPLOAD AND VIRAL METADATA
    log(f"\n📤 [STEP 2/3 & 3/3] Generating viral metadata and uploading to YouTube Shorts...")
    res = upload_batch(count=count)

    log("\n" + "═" * 60)
    log("🎉 DAILY PIPELINE COMPLETE!")
    log("═" * 60 + "\n")
    return res


def main():
    parser = argparse.ArgumentParser(description="Autonomous Daily Pipeline (Generation + Upload)")
    parser.add_argument("--count", type=int, default=2, help="Number of videos to generate and publish per day (default: 2)")
    parser.add_argument("--lang", choices=["en-GB", "en-US"], default="en-GB", help="Language of the generated videos (default: en-GB)")
    parser.add_argument("--generate-only", action="store_true", help="Only generate videos, don't upload")
    parser.add_argument("--upload-only", action="store_true", help="Only upload pending videos")
    parser.add_argument("--status", action="store_true", help="Show video status and upload history")

    args = parser.parse_args()

    if args.status:
        show_status()
        return 0

    return asyncio.run(run_daily_pipeline(
        count=args.count,
        generate_only=args.generate_only,
        upload_only=args.upload_only,
        lang=args.lang
    ))


if __name__ == "__main__":
    sys.exit(main())
