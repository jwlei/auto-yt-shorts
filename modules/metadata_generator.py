"""
YouTube Shorts metadata generator using Gemini AI.
Creates SEO-optimized titles, descriptions and tags automatically.
"""

import os
import json
from dotenv import load_dotenv

# Shares the brain's model chain, so a used-up daily quota hands over to the next model.
from modules.brain import _generate

load_dotenv()


def generate_metadata(video_filename, target_lang="en-GB"):
    """
    Generates metadata (title, description, tags) using Gemini AI
    based on the video file name and target language.

    Args:
        video_filename: Name of the video file.
        target_lang: 'en-GB' (British English) or 'en-US' (American English).

    Returns:
        Dictionary with 'title', 'description', 'tags', 'language' and 'filename'.
    """
    topic_hint = os.path.splitext(video_filename)[0]
    topic_hint = topic_hint.replace("_", " ").replace("-", " ")

    prompt = f"""
    You are the world's BEST YouTube Shorts virality expert.
    Your mission: create highly viral metadata for the YouTube algorithm.

    Video file name: "{video_filename}"
    Topic: "{topic_hint}"
    Target language: "{target_lang}"

    ### RULES FOR GOING VIRAL:

    1. **Language**:
       - Title, Description and Tags in ENGLISH (UK spelling for 'en-GB', US spelling for 'en-US'). Add `"language": "{target_lang}"`.

    2. **Title** (max 60 characters):
       - Use psychological triggers (curiosity, shock, surprise) and a strong leading emoji (🤯, ⚡, 😱, 🔥, 💀, ❌).

    3. **Description**:
       - Captivating, with a Call-to-Action and relevant hashtags, ALWAYS including #Shorts #Viral #FYP #ForYou.

    4. **Tags** (15-20 viral tags):
       - Focused on the language and a global English-speaking audience.

    ### OUTPUT FORMAT (plain JSON, no markdown):
    {{
        "title": "Title with emoji",
        "description": "Description with hashtags",
        "tags": ["tag1", "tag2", ...],
        "language": "{target_lang}"
    }}

    Reply ONLY with the valid JSON, nothing else.
    """

    print(f"🤖 Generating metadata for '{video_filename}'...")

    # Try up to 3 times in case of malformed JSON
    for attempt in range(3):
        try:
            response = _generate(prompt)

            raw_text = response.text.strip()

            # Strip markdown formatting if present
            if raw_text.startswith("```json"):
                raw_text = raw_text[7:]
            if raw_text.startswith("```"):
                raw_text = raw_text[3:]
            if raw_text.endswith("```"):
                raw_text = raw_text[:-3]

            raw_text = raw_text.strip()
            metadata = json.loads(raw_text)

            # Basic validation
            if not all(k in metadata for k in ('title', 'description', 'tags')):
                print(f"   ⚠️  Incomplete JSON (attempt {attempt + 1}/3)")
                continue

            # Make sure #Shorts is in the description
            if "#Shorts" not in metadata['description']:
                metadata['description'] += "\n\n#Shorts"

            metadata['filename'] = video_filename
            print(f"   ✅ Title: {metadata['title']}")
            return metadata

        except json.JSONDecodeError as e:
            print(f"   ⚠️  Invalid JSON (attempt {attempt + 1}/3): {e}")
            continue
        except Exception as e:
            print(f"   ⚠️  API error (attempt {attempt + 1}/3): {e}")
            continue

    # Fallback with generic metadata
    print("   ⚠️  Using default metadata (fallback)")
    return {
        "title": f"✨ New Short: {topic_hint[:50]}",
        "description": f"Watch our new Short about {topic_hint}!\n\n#Shorts #facts #trivia",
        "tags": ["shorts", "facts", "trivia", "education", "viral"],
        "filename": video_filename
    }


def generate_metadata_batch(video_dir):
    """
    Generates metadata for every .mp4 video in a directory.
    Saves the results to a JSON file for review before uploading.

    Args:
        video_dir: Path to the directory containing the videos.

    Returns:
        List of metadata dictionaries, one per video.
    """
    if not os.path.exists(video_dir):
        print(f"❌ Directory not found: {video_dir}")
        return []

    videos = sorted([f for f in os.listdir(video_dir) if f.lower().endswith('.mp4')])

    if not videos:
        print(f"⚠️  No .mp4 videos found in {video_dir}")
        return []

    print(f"🔍 Found {len(videos)} videos. Starting metadata generation...\n")

    metadata_list = []
    for i, video in enumerate(videos, 1):
        print(f"[{i}/{len(videos)}] Processing...")
        meta = generate_metadata(video)
        metadata_list.append(meta)
        print()

    # Save to JSON for manual review before uploading
    output_file = os.path.join(video_dir, "metadata_review.json")
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(metadata_list, f, ensure_ascii=False, indent=4)

    print(f"💾 Metadata saved for review at: {output_file}")
    print(f"   📋 You can edit the JSON file before running the upload.\n")
    return metadata_list


def load_metadata(meta_file):
    """
    Loads metadata from a previously saved JSON file.

    Args:
        meta_file: Path to the JSON file.

    Returns:
        List of metadata dictionaries.
    """
    if not os.path.exists(meta_file):
        print(f"❌ Metadata file not found: {meta_file}")
        return []

    with open(meta_file, 'r', encoding='utf-8') as f:
        metadata_list = json.load(f)

    print(f"📂 Metadata loaded from {meta_file} ({len(metadata_list)} entries)")
    return metadata_list


# --- MODULE TEST ---
if __name__ == "__main__":
    import sys

    test_dir = sys.argv[1] if len(sys.argv) > 1 else "0_output"
    print(f"🧪 Testing metadata generation for videos in: {test_dir}\n")
    results = generate_metadata_batch(test_dir)

    if results:
        print("\n📊 Results:")
        for meta in results:
            print(f"  🎬 {meta['filename']}")
            print(f"     Title: {meta['title']}")
            print(f"     Tags: {', '.join(meta['tags'][:5])}...")
            print()
