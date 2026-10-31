import os

import ffmpeg

from modules.avatar_generator import pick_reference_image
from modules.paths import avatar_still_path, thumbnail_path

# YouTube refuses thumbnails over this size.
THUMBNAIL_MAX_BYTES = 2 * 1024 * 1024
# Each try costs one image from the model's quota.
THUMBNAIL_ATTEMPTS = 2
# Her face, from forehead to chin, as a part of the thumbnail's height. The prompt asks
# for her head to fill about half of it; below this her face is no longer the focus.
MIN_FACE_SHARE = 0.25


def save_avatar_still(video_path, clips=(), reference_image=None):
    """
    Saves the still her thumbnail is built from, beside the video: the frame of its
    accepted clips that best faces the camera, else a crop of the reference image.
    clips: [(clip path, seconds shown)]. Returns the still's path, or None without one.
    """
    # Imported here so runs without lip-sync never need mediapipe installed.
    from modules.lip_check import best_face_frame, crop_to_face

    path = avatar_still_path(video_path)
    if clips and best_face_frame(clips, path):
        print(f"   🖼️ Saved a still of the avatar for the thumbnail: {os.path.basename(path)}")
        return path
    image = reference_image or pick_reference_image()
    if image and crop_to_face(image, path):
        print(f"   🖼️ Using {os.path.basename(image)} as the avatar still for the thumbnail.")
        return path
    return None


def find_avatar_still(video_path):
    """The still saved with the video, or one from a reference image for a video without. None if neither works."""
    path = avatar_still_path(video_path)
    if os.path.exists(path):
        return path
    try:
        return save_avatar_still(video_path)
    except Exception as e:
        print(f"   ⚠️ Could not make a still of the avatar: {e}")
        return None


def _save_jpeg(image, path):
    """Writes the image as a JPEG within YouTube's limit. Returns the path, or None if no quality fits."""
    for quality in (2, 5, 10):  # JPEG scale: lower is better
        ffmpeg.input(image).output(path, vframes=1, **{'q:v': quality}).run(overwrite_output=True, quiet=True)
        if os.path.getsize(path) <= THUMBNAIL_MAX_BYTES:
            return path
    return None


def _face_problem(image):
    """Returns what is wrong with her face in the image, or None if nothing is or it can't be measured, which accepts it."""
    try:
        from modules.lip_check import face_share

        share = face_share(image)
    except Exception as e:
        print(f"      ⚠️ Could not check the thumbnail, keeping it unchecked: {e}")
        return None
    if share is None:
        return "her face can't be seen"
    if share < MIN_FACE_SHARE:
        return f"her face is too small, {share:.2f}"
    return None


def make_thumbnail(still, video_path, topic, metadata, brain):
    """
    Has Gemini build the thumbnail from the avatar still and saves it beside the video.
    If no image comes back with her face clearly the focus, the still itself is the
    thumbnail, as she has to be on it either way. Returns None if nothing fits YouTube's limit.
    """
    path = thumbnail_path(video_path)
    generated = f"{os.path.splitext(path)[0]}.generated.png"
    try:
        for attempt in range(1, THUMBNAIL_ATTEMPTS + 1):
            image = brain.generate_thumbnail(still, topic, metadata)
            if not image:
                break
            with open(generated, "wb") as f:
                f.write(image)
            problem = _face_problem(generated)
            if not problem:
                return _save_jpeg(generated, path)
            print(f"      ⚠️ Thumbnail rejected: {problem} (attempt {attempt}/{THUMBNAIL_ATTEMPTS}).")
    finally:
        if os.path.exists(generated):
            os.remove(generated)
    print("   ⚠️ No usable thumbnail from Gemini: using the still of the avatar as it is.")
    return _save_jpeg(still, path)
