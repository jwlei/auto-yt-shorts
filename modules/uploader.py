"""
YouTube upload module using the Data API v3.
Supports OAuth2 authentication, resumable uploads and retry logic with exponential backoff.
"""

import os
import time
import random
import pickle

from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError


# Scopes required to upload to YouTube
SCOPES = ['https://www.googleapis.com/auth/youtube.upload']

# Default paths for the credential files
CLIENT_SECRETS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'client_secrets.json')
TOKEN_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'token.pickle')

# Retry settings
MAX_RETRIES = 10
RETRYABLE_STATUS_CODES = [500, 502, 503, 504]
CHUNK_SIZE = 1024 * 1024  # 1MB - must be a multiple of 256KB


def authenticate():
    """
    Authenticates the user with OAuth2 (desktop app flow).
    Saves the token to 'token.pickle' for reuse.
    Returns the YouTube API client, or None on failure.
    """
    creds = None

    # Try to load a previously saved token
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, 'rb') as token:
                creds = pickle.load(token)
            print("🔐 Token loaded from cache.")
        except Exception as e:
            print(f"⚠️  Error loading saved token: {e}")
            creds = None

    # Without valid credentials, the user needs to authenticate
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("🔄 Refreshing expired access token...")
            try:
                creds.refresh(Request())
            except Exception as e:
                print(f"⚠️  Failed to refresh token: {e}")
                print("🔑 You will need to authenticate again.")
                creds = None

        if not creds or not creds.valid:
            # Check that the credentials file exists
            if not os.path.exists(CLIENT_SECRETS_FILE):
                print("❌ Error: 'client_secrets.json' file not found!")
                print()
                print("📋 To set up authentication:")
                print("   1. Go to https://console.cloud.google.com/")
                print("   2. Create a new project (or use an existing one)")
                print("   3. Enable the 'YouTube Data API v3'")
                print("   4. Go to 'Credentials' → 'Create credentials' → 'OAuth client ID'")
                print("   5. Application type: 'Desktop app'")
                print("   6. Download the JSON and save it as 'client_secrets.json' in the project root")
                print(f"   7. Expected path: {CLIENT_SECRETS_FILE}")
                return None

            print("🔑 Starting OAuth2 authentication flow...")
            flow = InstalledAppFlow.from_client_secrets_file(
                CLIENT_SECRETS_FILE, SCOPES,
                redirect_uri='urn:ietf:wg:oauth:2.0:oob'
            )
            auth_url, _ = flow.authorization_url(prompt='consent')
            print()
            print("   📋 Open this URL in the browser on your phone/PC:")
            print(f"   {auth_url}")
            print()
            code = input("   🔑 Paste the authorization code here: ").strip()
            flow.fetch_token(code=code)
            creds = flow.credentials



        # Save the credentials for future runs
        try:
            with open(TOKEN_FILE, 'wb') as token:
                pickle.dump(creds, token)
            print("✅ Token saved successfully to 'token.pickle'!")
        except Exception as e:
            print(f"⚠️  Warning: Could not save the token: {e}")

    youtube = build('youtube', 'v3', credentials=creds)
    print("✅ Successfully authenticated with the YouTube API!")
    return youtube


def _resumable_upload(request, video_path):
    """
    Runs the resumable upload with exponential backoff.
    Retries automatically on server (5xx) or network errors.
    """
    response = None
    retry = 0
    filename = os.path.basename(video_path)

    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                progress = int(status.progress() * 100)
                print(f"   📈 Progress: {progress}%")
        except HttpError as e:
            status_code = e.resp.status

            if status_code in RETRYABLE_STATUS_CODES:
                retry += 1
                if retry > MAX_RETRIES:
                    print(f"   ❌ Maximum retries ({MAX_RETRIES}) reached.")
                    raise

                sleep_time = (2 ** retry) + random.uniform(0, 1)
                print(f"   ⚠️  Server error ({status_code}). Retrying in {sleep_time:.1f}s...")
                time.sleep(sleep_time)

            elif status_code == 403:
                error_reason = ""
                try:
                    import json
                    error_detail = json.loads(e.content.decode('utf-8'))
                    error_reason = error_detail.get('error', {}).get('errors', [{}])[0].get('reason', '')
                except Exception:
                    pass

                if error_reason in ('quotaExceeded', 'dailyLimitExceeded'):
                    print(f"   ❌ API quota exceeded! ({error_reason})")
                    print("   ⏳ The daily quota resets at midnight Pacific Time (PT).")
                    print("   💡 Tip: You can request a quota increase in the Google Cloud Console.")
                else:
                    print(f"   ❌ Permission error (403): {e}")
                raise

            elif status_code == 404:
                print("   ⚠️  Upload session expired. Recreating...")
                # Recreate the upload - the caller must handle this
                raise

            else:
                print(f"   ❌ Unrecoverable HTTP error ({status_code}): {e}")
                raise

        except (OSError, ConnectionError, ConnectionResetError) as e:
            retry += 1
            if retry > MAX_RETRIES:
                print(f"   ❌ Maximum retries reached due to connection failure.")
                raise

            sleep_time = (2 ** retry) + random.uniform(0, 1)
            print(f"   ⚠️  Network error ({type(e).__name__}). Retrying in {sleep_time:.1f}s...")
            time.sleep(sleep_time)

    return response


def upload_video(youtube, video_path, title, description, tags,
                 category_id=27, privacy='public',
                 default_language=None, default_audio_language=None):
    """
    Uploads a single video to YouTube.

    Args:
        youtube: Authenticated YouTube API client.
        video_path: Full path to the video file.
        title: Video title.
        description: Video description.
        tags: List of tags (keywords).
        category_id: Category ID (27 = Education).
        privacy: 'public', 'unlisted' or 'private'.
        default_language: Default metadata language code (e.g. 'en-GB', 'en-US').
        default_audio_language: Default audio language code (e.g. 'en-GB', 'en-US').

    Returns:
        API response with the uploaded video's data, or None on failure.
    """
    if not os.path.exists(video_path):
        print(f"   ❌ File not found: {video_path}")
        return None

    # Make sure #Shorts is in the description so it is classified as a Short
    if "#Shorts" not in description:
        description = f"{description.strip()}\n\n#Shorts"

    # File size for reference
    file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
    print(f"   📁 File: {os.path.basename(video_path)} ({file_size_mb:.1f} MB)")
    print(f"   📝 Title: {title}")
    print(f"   🔒 Privacy: {privacy}")
    if default_language or default_audio_language:
        print(f"   🌐 Target language: {default_language or default_audio_language}")

    snippet = {
        'title': title,
        'description': description,
        'tags': tags,
        'categoryId': str(category_id)
    }

    if default_language:
        snippet['defaultLanguage'] = default_language
    if default_audio_language:
        snippet['defaultAudioLanguage'] = default_audio_language

    body = {
        'snippet': snippet,
        'status': {
            'privacyStatus': privacy,
            'selfDeclaredMadeForKids': False
        }
    }

    media = MediaFileUpload(
        video_path,
        chunksize=CHUNK_SIZE,
        resumable=True
    )

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media
    )

    print(f"   🚀 Starting upload...")

    try:
        response = _resumable_upload(request, video_path)
    except HttpError as e:
        if e.resp.status == 404:
            # Session expired, start over
            print("   🔄 Recreating upload session...")
            media = MediaFileUpload(video_path, chunksize=CHUNK_SIZE, resumable=True)
            request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
            response = _resumable_upload(request, video_path)
        else:
            raise

    if response:
        video_id = response.get('id')
        print(f"   🎉 Upload complete! ID: {video_id}")
        print(f"   🔗 Link: https://youtu.be/{video_id}")
        return response

    return None


# --- MODULE TEST ---
if __name__ == "__main__":
    print("🧪 Testing authentication...")
    yt = authenticate()
    if yt:
        print("✅ Authentication OK! Ready for uploads.")
    else:
        print("❌ Authentication failed.")
