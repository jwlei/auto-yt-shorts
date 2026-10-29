import argparse
import asyncio
import os
import shutil
import sys
from datetime import datetime

from dotenv import load_dotenv

from modules.brain import ContentBrain
from modules.asset_manager import AssetManager, PROVIDER_KEYS, get_provider
from modules.audio import AudioEngine, get_tts_provider
from modules.avatar_generator import AVATAR_CLIP_COUNTS, AvatarGenerator, default_avatar_clips, get_avatar_source
from modules.composer import Composer
from modules.paths import move_to_uploaded

load_dotenv()


def clean_cache():
    """
    Safely deletes temporary files.
    Includes a Safety Lock to prevent deleting anything outside the project.
    """
    print("🧹 Cleaning up temporary files...")

    # 1. Define the specific target folders
    folders_to_clean = [
        os.path.join(os.getcwd(), "assets", "audio_clips"),
        os.path.join(os.getcwd(), "assets", "video_clips"),
        os.path.join(os.getcwd(), "assets", "temp")
    ]

    for folder in folders_to_clean:
        # SAFETY CHECK 1: Ensure folder actually exists
        if not os.path.exists(folder):
            continue

        # SAFETY CHECK 2: Double check we are inside our project "assets" folder
        # This prevents the script from ever touching C:\ or System32
        if "assets" not in folder:
            print(f"   🚨 SECURITY ALERT: Skipping {folder} because it looks unsafe!")
            continue

        # Loop through files inside the folder
        for filename in os.listdir(folder):
            file_path = os.path.join(folder, filename)

            try:
                if os.path.isfile(file_path) or os.path.islink(file_path):
                    os.unlink(file_path) # Delete the file
                    print(f"      Deleted: {filename}") # Print so you can see it working
                elif os.path.isdir(file_path):
                    shutil.rmtree(file_path) # Delete subfolders if any
            except Exception as e:
                print(f"   ❌ Failed to delete {file_path}. Reason: {e}")

    print("✨ Workspace clean!")


def preflight(args=None):
    """
    Verifies everything an unattended run depends on. Returns a list of problems;
    an empty list means the pipeline is safe to start.
    """
    problems = []

    if args is not None and args.upload:
        try:
            import modules.publisher  # noqa: F401
        except ImportError as e:
            problems.append(f"--upload needs the Google client libraries ({e}). "
                            "Run: pip install -r requirements.txt")
        for key in ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"):
            if not os.getenv(key):
                problems.append(f"{key} is not set — run authorize_youtube.py once to mint it.")

    if not shutil.which("ffmpeg"):
        problems.append("ffmpeg not found on PATH — install it (see README Prerequisites).")
    if not shutil.which("ffprobe"):
        problems.append("ffprobe not found on PATH — it ships with ffmpeg.")

    required = ["GEMINI_API_KEY"]
    try:
        provider = get_provider()
        required.append(PROVIDER_KEYS[provider])
    except RuntimeError as e:
        problems.append(str(e))
    try:
        get_tts_provider()
    except RuntimeError as e:
        problems.append(str(e))
    for key in required:
        if not os.getenv(key):
            problems.append(f"{key} is not set — add it to your .env file.")

    avatar = os.path.join(os.getcwd(), "assets", "avatar", "avatars.mp4")
    try:
        default_avatar_clips()
        avatar_source = get_avatar_source()
    except RuntimeError as e:
        problems.append(str(e))
        avatar_source = None

    if avatar_source == "comfyui":
        try:
            generator = AvatarGenerator()
            if not generator.is_reachable():
                # Not fatal: avatar scenes fall back to the static clip or stock footage.
                print(f"⚠️ ComfyUI is not reachable at {generator.url} — avatar scenes will fall back.")
        except RuntimeError as e:
            problems.append(str(e))
    elif not os.path.exists(avatar):
        # Not fatal: the composer simply skips avatar injection.
        print(f"⚠️ Avatar clip missing at {avatar} — continuing without avatar injection.")

    return problems


STAGES = ["Topic & script", "Voice", "Stock footage", "Avatar clips", "Rendering scenes", "Stitching", "Upload"]


def stage(name, upload):
    """
    Prints a machine-readable stage marker ("▶ Stage 2/6: Voice"); the web UI
    turns these into its progress stepper.
    """
    stages = STAGES if upload else STAGES[:-1]
    print(f"▶ Stage {stages.index(name) + 1}/{len(stages)}: {name}")


def build_output_name(prefix="short"):
    return f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"


async def generate_one(args):
    """
    Runs the full pipeline once. Returns the final video path, or None on failure.
    """
    lang = getattr(args, 'lang', 'en-GB')
    upload = getattr(args, 'upload', False)

    # 1. BRAIN: Get Script
    stage("Topic & script", upload)
    brain = ContentBrain()
    try:
        topic = args.topic or brain.get_trending_topic()
        if args.topic:
            print(f"🎯 Using provided topic: {topic}")
        script = brain.generate_script(topic, lang=lang)
    except Exception as e:
        print(f"❌ Brain Error: {e}")
        return None

    if not script:
        print("❌ Script generation failed.")
        return None

    # 2. AUDIO: Generate Voice
    stage("Voice", upload)
    audio_engine = AudioEngine(voice=args.voice, lang=lang)
    try:
        script = await audio_engine.process_script(script)
    except Exception as e:
        print(f"❌ Audio Error: {e}")
        return None

    if not script:
        print("❌ No scene produced audio.")
        return None

    # 3. ASSETS: Get Stock Video
    stage("Stock footage", upload)
    asset_manager = AssetManager()
    assets_map = asset_manager.get_videos(script)

    # 4. COMPOSER: Merge Video + Audio
    composer = Composer()

    # 4a. AVATAR: Render presenter clips for the hook + two middle scenes, or just
    # the two middle scenes with --avatar-clips 2 (ComfyUI)
    avatar_count = getattr(args, 'avatar_clips', None)
    if avatar_count is None:
        avatar_count = default_avatar_clips()
    avatar_clips = None
    stage("Avatar clips", upload)
    if get_avatar_source() == "comfyui" and avatar_count:
        indices = composer.pick_avatar_indices(script, avatar_count)
        try:
            avatar_clips = AvatarGenerator().generate(script, indices, brain)
        except Exception as e:
            print(f"❌ Avatar Error: {e}")
            avatar_clips = {}
        # A failed render still gets an avatar if the static clip exists; otherwise stock footage.
        for i in indices:
            if i not in avatar_clips and os.path.exists(composer.avatar_path):
                avatar_clips[i] = composer.avatar_path

    stage("Rendering scenes", upload)
    final_scene_paths = composer.render_all_scenes(script, assets_map, avatar_clips, avatar_count)

    # 5. STITCH WITH TRANSITIONS
    if not final_scene_paths:
        print("❌ Failed to generate any scenes.")
        return None

    stage("Stitching", upload)
    output_name = args.output if (args.output and args.runs == 1) else build_output_name()
    final_path = composer.concatenate_with_transitions(final_scene_paths, output_name)

    # 6. PUBLISH (optional)
    if final_path and args.upload:
        stage("Upload", upload)
        metadata = brain.generate_metadata(topic, script)
        try:
            # Imported here so the render-only path never needs the Google
            # client libraries installed.
            from modules.publisher import YouTubeUploader

            url = YouTubeUploader().upload(
                final_path,
                title=metadata['title'],
                description=metadata['description'],
                tags=metadata['tags'],
                privacy_status=args.privacy,
            )
            if url:
                # 0_output only holds videos still waiting to go up.
                final_path = move_to_uploaded(final_path)
        except Exception as e:
            # The video rendered fine; a failed upload should not discard it.
            print(f"❌ Upload Error: {e}")

    if final_path and not args.keep_cache:
        clean_cache()

    return final_path


async def main_async(args):
    if args.check:
        problems = preflight(args)
        for problem in problems:
            print(f"❌ {problem}")
        if problems:
            return 1
        print("✅ Preflight passed — ready to run.")
        return 0

    problems = preflight(args)
    if problems:
        for problem in problems:
            print(f"❌ {problem}")
        return 1

    produced = []
    for run_index in range(args.runs):
        if args.runs > 1:
            print(f"\n===== RUN {run_index + 1}/{args.runs} =====")
        print("🚀 STARTING AUTOMATION...")

        try:
            final_path = await generate_one(args)
        except Exception as e:
            print(f"❌ Unexpected failure: {e}")
            final_path = None

        if final_path:
            produced.append(final_path)
        elif args.fail_fast:
            print("🛑 Stopping: --fail-fast is set.")
            break

    print(f"\n📦 Produced {len(produced)}/{args.runs} video(s).")
    for path in produced:
        print(f"   • {path}")

    # Non-zero exit tells cron/CI the run needs attention.
    return 0 if len(produced) == args.runs else 1


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Auto YT Shorts — generate faceless YouTube Shorts end-to-end."
    )
    parser.add_argument("--topic", help="Skip the AI topic picker and use this topic.")
    parser.add_argument("--runs", type=int, default=1, help="How many videos to generate (default: 1).")
    parser.add_argument("--lang", choices=["en-GB", "en-US"], default="en-GB", help="Language for voiceover and script (default: en-GB).")
    parser.add_argument("--voice", help="Gemini TTS voice (e.g. en-gb-tutor-13) or edge-tts voice (e.g. en-GB-SoniaNeural). Default depends on --lang and TTS_PROVIDER.")
    parser.add_argument("--output", help="Output filename for a single run (default: timestamped).")
    parser.add_argument("--keep-cache", action="store_true", help="Keep intermediate audio/video files.")
    parser.add_argument("--fail-fast", action="store_true", help="Stop the batch on the first failed run.")
    parser.add_argument("--check", action="store_true", help="Run preflight checks and exit.")
    parser.add_argument("--upload", action="store_true", help="Upload each finished video to YouTube.")
    parser.add_argument("--privacy", choices=("private", "unlisted", "public"), default="private",
                        help="Privacy status for uploads (default: private).")
    parser.add_argument("--avatar-clips", type=int, choices=AVATAR_CLIP_COUNTS,
                        help="3 = open on the avatar plus two middle scenes; 2 = two middle scenes only; 0 = no avatar "
                             "(default: AVATAR_CLIPS from .env, else 3).")

    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    return args


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async(parse_args())))
