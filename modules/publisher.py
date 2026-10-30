import os
import time

import ffmpeg
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from modules.api_log import log_api_call, log_api_error
from modules.paths import move_to_uploaded, thumbnail_path

# Uploading is the only scope this needs (it covers setting a thumbnail too).
# Keeping it narrow means the consent screen cannot hand out read/write access
# to the rest of the channel.
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

TOKEN_URI = "https://oauth2.googleapis.com/token"

# YouTube refuses thumbnails over this size.
THUMBNAIL_MAX_BYTES = 2 * 1024 * 1024
# One frame per second is considered, for up to this many seconds. Each frame
# considered is held in memory.
THUMBNAIL_SAMPLES = 60


def make_thumbnail(video_path):
    """
    Saves a still from the video beside it and returns its path. ffmpeg's thumbnail
    filter picks the frame most typical of those it looks at, which passes over
    black frames and most of a transition. Returns None if no quality fits the limit.
    """
    path = thumbnail_path(video_path)
    for quality in (2, 5, 10):  # JPEG scale: lower is better
        (
            ffmpeg.input(video_path)
            .filter('fps', fps=1)
            .filter('thumbnail', n=THUMBNAIL_SAMPLES)
            .output(path, vframes=1, **{'q:v': quality})
            .run(overwrite_output=True, quiet=True)
        )
        if os.path.getsize(path) <= THUMBNAIL_MAX_BYTES:
            return path
    return None


class YouTubeUploader:
    """
    Uploads a finished short to YouTube using a long-lived refresh token, so an
    unattended run never needs a browser. Run scripts/authorize_youtube.py once to mint
    that token.
    """

    def __init__(self):
        self.client_id = os.getenv("YOUTUBE_CLIENT_ID")
        self.client_secret = os.getenv("YOUTUBE_CLIENT_SECRET")
        self.refresh_token = os.getenv("YOUTUBE_REFRESH_TOKEN")

        missing = [
            name for name, value in (
                ("YOUTUBE_CLIENT_ID", self.client_id),
                ("YOUTUBE_CLIENT_SECRET", self.client_secret),
                ("YOUTUBE_REFRESH_TOKEN", self.refresh_token),
            ) if not value
        ]
        if missing:
            raise RuntimeError(
                f"{', '.join(missing)} not set. Run scripts/authorize_youtube.py once and "
                "copy the values it prints into your .env file."
            )

    def _service(self):
        credentials = Credentials(
            token=None,
            refresh_token=self.refresh_token,
            client_id=self.client_id,
            client_secret=self.client_secret,
            token_uri=TOKEN_URI,
            scopes=SCOPES,
        )
        credentials.refresh(Request())
        return build("youtube", "v3", credentials=credentials, cache_discovery=False)

    def upload(self, video_path, title, description, tags=None, privacy_status="private",
               category_id="27"):
        """
        Uploads video_path and returns the new video's URL, or None on failure.

        privacy_status defaults to 'private' on purpose: an unverified Google
        Cloud project has its uploads forced private anyway, and it gives you a
        chance to review before anything goes live.
        """
        if not os.path.exists(video_path):
            print(f"❌ Upload skipped: {video_path} does not exist.")
            return None

        # YouTube truncates past these limits, so trim deliberately instead.
        title = title[:100]
        description = description[:5000]

        body = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": (tags or [])[:15],
                "categoryId": category_id,
            },
            "status": {
                "privacyStatus": privacy_status,
                "selfDeclaredMadeForKids": False,
            },
        }

        print(f"📤 Uploading to YouTube as '{privacy_status}': {title}")

        try:
            service = self._service()
        except Exception as e:
            print(f"❌ YouTube auth failed: {e}")
            return None

        media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype="video/mp4")
        request = service.videos().insert(part="snippet,status", body=body, media_body=media)

        response = None
        errors = 0
        while response is None:
            try:
                _, response = request.next_chunk()
            except HttpError as e:
                # 5xx is transient; anything else (quota, bad metadata) will not
                # get better by trying again.
                if e.resp.status in (500, 502, 503, 504) and errors < 3:
                    errors += 1
                    wait = 2 ** errors
                    print(f"   ⚠️ Transient upload error ({e.resp.status}). Retrying in {wait}s...")
                    time.sleep(wait)
                    continue
                log_api_error("YouTube", "uploads", e)
                print(f"❌ Upload failed: {e}")
                return None
            except Exception as e:
                print(f"❌ Upload failed: {e}")
                return None

        video_id = response.get("id")
        if not video_id:
            print(f"❌ Upload returned no video id: {response}")
            return None

        log_api_call("YouTube", "uploads")
        url = f"https://youtube.com/shorts/{video_id}"
        print(f"✅ Uploaded: {url}")
        return url

    def set_thumbnail(self, video_id, image_path):
        """Sets the video's thumbnail. Returns whether it worked; the video is up either way."""
        try:
            media = MediaFileUpload(image_path, mimetype="image/jpeg")
            self._service().thumbnails().set(videoId=video_id, media_body=media).execute()
        except HttpError as e:
            log_api_error("YouTube", "thumbnails", e)
            if e.resp.status == 403:
                print("   ⚠️ YouTube refused the thumbnail. Custom thumbnails need a verified channel: "
                      "https://www.youtube.com/verify")
            return False
        except Exception as e:
            print(f"   ⚠️ Could not set the thumbnail: {e}")
            return False
        log_api_call("YouTube", "thumbnails")
        print(f"   🖼️ Thumbnail set from {os.path.basename(image_path)}")
        return True


def _upload_thumbnail(uploader, video_path, url):
    """A thumbnail is a bonus: failing to make one must not count against the finished upload."""
    try:
        image_path = make_thumbnail(video_path)
    except (ffmpeg.Error, OSError) as e:
        print(f"   ⚠️ Could not make a thumbnail: {e}")
        return
    if not image_path:
        print("   ⚠️ No thumbnail: the still stayed over YouTube's 2 MB limit.")
        return
    uploader.set_thumbnail(url.rsplit("/", 1)[-1], image_path)


def publish_video(video_path, topic, narration, privacy_status, brain):
    """
    Has Gemini write the title, description and tags, uploads the video, moves it
    to 1_uploaded (0_output only holds videos still waiting to go up), and sets a
    still from it as the thumbnail, kept beside the video.
    Returns (path, url): the url is None and the path unchanged if it failed.
    """
    metadata = brain.generate_metadata(topic, narration)
    uploader = YouTubeUploader()
    url = uploader.upload(
        video_path,
        title=metadata['title'],
        description=metadata['description'],
        tags=metadata['tags'],
        privacy_status=privacy_status,
    )
    if url:
        video_path = move_to_uploaded(video_path)
        _upload_thumbnail(uploader, video_path, url)
    return video_path, url
