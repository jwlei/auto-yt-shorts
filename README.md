# Auto YT Shorts

Automated faceless YouTube Shorts: AI script, natural narration, stock footage and a lip-synced AI presenter.

**Auto YT Shorts** is a Python pipeline that creates viral-style "Faceless" YouTube Shorts and TikToks from a topic. It handles the production chain: AI topic/script generation, voiceover generation, stock footage sourcing, and FFmpeg editing with transitions and avatar injection.

---

## Key Features

- **Intelligent Scriptwriting:** Uses **Google Gemini 2.0 Flash** to write engaging, "Edutainment" style scripts (Vox/Kurzgesagt style) with strict storytelling structures (Hook → Context → Mechanism → Twist).
- **Voiceovers:** Natural narration with **Gemini 3.8 Flash TTS** (British female presenter voice by default, delivery matched to each scene's mood). If it doesn't respond in time the video is re-voiced with Gemini 3.8 Flash Lite TTS in the same voice; if every Gemini model fails the run fails rather than using a robotic voice.
- **Dual-Visual System:** Automatically searches and downloads **two distinct stock videos** per scene from **Pixabay** or **Pexels** (your choice via `VIDEO_PROVIDER`), creating a dynamic "A/B Split" visual style to maximize viewer retention.
- **Advanced FFmpeg Editing:**
- **Smart Trimming:** Syncs video perfectly to audio duration.
- **A/B Splitting:** Cuts every scene in half, switching visuals mid-sentence.
- **Pro Transitions:** Randomly applies `xfade` (fade, slide, wipes) between scenes.
- **Silence Removal:** Automatically trims dead air from AI voice generation.

- **Avatar Injection:** Opens on your presenter and brings her back for 3-5 scenes in total (adjustable), giving her the scenes where the stock footage fits the narration worst (Gemini rates the footage once it is downloaded) — generated and lip-synced with ComfyUI.
- **Windows Ready:** Includes specific FFmpeg flags (`yuv420p`, `faststart`) to prevent corruption errors (`0x80004005`) on Windows Media Player.

---

## Project Structure

```text
auto-yt-shorts/
│
├── 0_output/               # Finished videos waiting to be uploaded
├── 1_uploaded/             # Videos moved here once uploaded, each with its thumbnail (+ upload_history.json)
├── 2_badOutput/            # Videos rejected after watching, and notes.md: what was wrong and what changed
├── comfyui/                # ComfyUI avatar workflows (API format) + node maps
├── logs/                   # api_errors.log — every API error with the server's message; api_usage.jsonl — calls counted for the quota list
├── assets/                  # Stores all media files
│   ├── audio_clips/         # Generated voiceovers (.wav)
│   ├── video_clips/         # Downloaded stock footage (.mp4)
│   ├── temp/                # Intermediate processing files
│   └── avatar/              # PUT YOUR AVATAR IMAGE (avatar.jpg) / VIDEO (avatars.mp4) HERE
│
├── modules/                 # Core Logic Modules
│   ├── brain.py             # AI Scriptwriter (Gemini)
│   ├── audio.py             # Voice generator (Gemini TTS, model fallback chain)
│   ├── asset_manager.py     # Pixabay/Pexels Downloader (Dual-Visual logic)
│   ├── avatar_generator.py  # ComfyUI presenter clips (lip-synced)
│   └── composer.py          # FFmpeg Video Editor (Stitching & Transitions)
│
├── documents/               # Code of conduct, contributing guide, security policy
├── scripts/                 # One-off setup: YouTube authorization, VM install, desktop shortcut
├── legacy/                  # The old upload scripts and scratch files; the panel and main.py don't use them
├── templates/               # The control panel's pages
│
├── webapp.py                # The control panel
├── start_webapp.pyw         # Starts the panel with no console window
├── publish.py               # Uploads finished videos
├── main.py                  # Entry point (Orchestrator)
└── requirements.txt         # Python dependencies

```

---

## Prerequisites

1. **Python 3.10+** installed.
2. **FFmpeg** installed and added to your system PATH.

- _Windows:_ `winget install ffmpeg` (or download from [ffmpeg.org](https://ffmpeg.org/download.html)).
- _Verify:_ Type `ffmpeg -version` in your terminal.

3. **API Keys:**

- **Google Gemini API Key** (Free tier available).
- **Pixabay API Key** (Free — https://pixabay.com/api/docs/) or **Pexels API Key** (Free — https://www.pexels.com/api/).
- No Ngrok token is required for the default voiceover path. The current pipeline uses Gemini TTS (same `GEMINI_API_KEY`); `edge-tts` is only used if you set `TTS_PROVIDER=edge`.

---

## Installation

### 1. Clone the Repository

```bash
git clone https://github.com/jwlei/auto-yt-shorts.git
cd AutoShorts-AI

```

### 2. Install Dependencies

```bash
pip install -r requirements.txt

```

### 3. Environment Setup

Create the required folders and add your avatar:

1. Create folder: `assets/avatar`
2. Place your avatar video inside and name it: `avatars.mp4`

### 4. Configure API Keys

Copy `.env.example` to `.env` and fill in your key:

```bash
cp .env.example .env
```

Required:

- `GEMINI_API_KEY` for script generation
- `PIXABAY_API_KEY` (default) or `PEXELS_API_KEY` for stock video search/download — whichever `VIDEO_PROVIDER` selects

Optional:

- `GEMINI_MODEL` to override the text model chain `gemini-3.8-flash,gemini-3.5-flash,gemini-3-flash-preview,gemini-3.5-flash-lite` (one model, or a comma-separated list tried in order when a model hits its daily quota or stops answering)
- `GEMINI_FAST_MODEL` to override the fast model chain used for small yes/no checks such as "was this topic already made?" (default `gemini-3.5-flash-lite,gemini-3.5-flash`)
- `GEMINI_TIMEOUT` seconds to wait for a text reply before moving on to the next model (default `90`); a model that times out or runs out of daily quota is skipped for the rest of the run
- `VIDEO_PROVIDER` to pick the stock video source: `pixabay` (default) or `pexels`
- `TTS_PROVIDER` to pick the voice engine: `gemini` (default, natural) or `edge` (free, more robotic)
- `GEMINI_TTS_VOICE` to always use a specific Gemini voice (browse the voice library with `client.voices.list(language_code=["en-GB"])`)
- `GEMINI_TTS_MODEL` to override the model chain `gemini-3.8-flash-tts,gemini-3.8-flash-lite-tts` (one model, or a comma-separated list tried in order)
- `GEMINI_TTS_TIMEOUT` seconds to wait for the narration before moving to the next model (default `120`)
- `GEMINI_TTS_RPM` requests per minute allowed per TTS model (default `3`, the free tier); only matters for the per-scene fallback
- `GEMINI_TTS_STYLE` to change the base delivery instruction given to every scene
- `AVATAR_SOURCE` to pick where avatar scenes come from: `static` (default, loops `avatars.mp4`) or `comfyui` (see below)

### 5. Optional: Generated Avatar Clips (ComfyUI)

With `AVATAR_SOURCE=comfyui`, the avatar scenes are rendered fresh by a local ComfyUI server instead of looping `avatars.mp4`. By default (`auto`) the video **opens on the avatar** (the hook) and she appears in 3-5 scenes in total (`AVATAR_CLIPS_MIN`/`MAX`, or the panel's *Clips per video* slider). The scenes are picked **after** the stock footage is downloaded: Gemini rates 0-10 how well each scene's footage matches its line, the total starts at a random number in the range and rises (never past its top) for every scene with poor footage (rated 4 or lower, or nothing found), and the weakest-footage scenes go to the avatar first, the rest at random weighted towards weaker footage. Fixed layouts are still available: `--avatar-clips 3` (hook + two random middle scenes), `2` (two middle scenes only) or `0` (none).

The presenter is **lip-synced** to the narration: each scene's voiceover is fed into the workflow and LTX generates her mouth and face to match it. Gemini writes a prompt for each clip that has her say the line to camera.

She stays on screen for a random 3-6 seconds each time (`AVATAR_MIN_SECONDS`/`MAX`, or the panel's *Clip length* slider); a longer line then cuts to that scene's stock footage while her voice carries on. So clips of one video don't look alike, each opens on its own framing of the reference image and gets its own camera move and gesture.

- `comfyui/avatar_lipsync.json` (default) is the workflow in API format (ComfyUI → Workflow → **Export (API)**): LTX-2.3 I2V/T2V plus three nodes that load the narration, encode it, and mask it so the audio is kept and only the video is generated.
- `comfyui/avatar_workflow.json` is the same workflow without lip-sync: the presenter reacts silently while the narration plays over her. Point `COMFYUI_WORKFLOW` at it to use it.
- Each `.map.json` says which node inputs to fill, as `"<node id>.<input>"`: `prompt` (required), `image`, `audio` (turns on lip-sync), `text_to_video`, `seconds`, `width`, `height`. Seeds are randomised automatically. If you swap in another workflow, update the map to match.
- Clips stay out of ComfyUI's `output` folder: any `save_output` input is switched off when a clip is queued, so ComfyUI saves it to its `temp` folder (which it wipes on every start) and the pipeline downloads it from there. Nothing is ever deleted from `output`, and renders you start in ComfyUI yourself save as usual. A swapped-in workflow must use a save node with `save_output`, such as Video Combine.

| Variable | Default | Purpose |
| --- | --- | --- |
| `AVATAR_CLIPS` | `auto` | `auto` = hook + a range of scenes chosen by footage fit; `3` = hook + two middle scenes; `2` = two middle scenes only; `0` = no avatar |
| `AVATAR_CLIPS_MIN`, `AVATAR_CLIPS_MAX` | `3`, `5` | Fewest and most avatar scenes per video with `auto` |
| `COMFYUI_URL` | `http://127.0.0.1:8188` | ComfyUI server (from Docker use `http://host.docker.internal:8188`) |
| `COMFYUI_WORKFLOW` | `comfyui/avatar_lipsync.json` | API workflow; its map is the same name with `.map.json` |
| `AVATAR_REFERENCE_IMAGE` | `assets/avatar/avatar*.jpg` | Persona image, or a glob of them: each video picks one at random and every clip in it is image-to-video from that image, so the persona stays the same within a video |
| `AVATAR_FOCUS_X` | `0.5` | Where to take the portrait crop from a wider reference image (0 = left edge, 1 = right edge). Set it to the presenter's horizontal position |
| `AVATAR_FOCUS_Y` | `0.3` | Height of her face in the reference image (0 = top, 1 = bottom), kept in view when a clip opens closer in |
| `AVATAR_PERSONA` | built-in description | Used when no reference image matches: the first clip is text-to-video from this, and a frame of it becomes the reference for the rest |
| `AVATAR_SIZE` | `736x1280` | Render size (portrait) |
| `AVATAR_MIN_SECONDS` | `3` | Shortest time she stays on screen each time |
| `AVATAR_MAX_SECONDS` | `6` | Longest time she stays on screen each time. A longer line cuts to stock footage while her voice carries on; a scene with no stock footage is covered whole, up to a 10 s render (or this value, if higher) |
| `COMFYUI_TIMEOUT` | `1800` | Seconds to wait per clip |

Each lip-synced clip is checked after rendering, on its worst second: if her mouth stops moving, her eyes go wide or her eye colour strays from the reference image, it is rendered again on a new seed, up to `AVATAR_LIPSYNC_ATTEMPTS` (default `3`) times, and the scene falls back to stock footage if it never passes. The limits come from the rejected videos in `2_badOutput/notes.md`. A clip whose mouth moves but doesn't match the words still passes: no check catches that yet. Because that is several renders per clip, a run can take longer when clips fail the check. The first check downloads a small face-tracking model to `assets/models/`; if the check can't run, the clip is kept and the log says so.

If a render fails, or a clip never passes the check, that scene uses its stock footage. With the silent workflow, a failed render falls back to `avatars.mp4` (if present) first; lip-synced scenes never do, since the static clip doesn't speak the line.

---

## How to Run

### Generate Video

Run the main script:

```bash
python main.py

```

1. The AI picks a trending topic, writes the script, generates the audio, downloads stock footage, and edits the video.
2. The final video is saved to `0_output/` with a timestamped name (e.g. `short_20260812_1930.mp4`).

### Automated / Unattended Runs

`main.py` is fully non-interactive and returns a non-zero exit code when a run
fails, so it drops straight into cron, systemd timers, or CI.

```bash
# Verify keys, ffmpeg and assets without generating anything
python main.py --check

# Pin the topic instead of letting the AI choose one
python main.py --topic "Why the Sahara was once green"

# Batch: produce 3 videos in one invocation
python main.py --runs 3

# Pick a voice and a fixed output name
python main.py --voice en-gb-storyteller-2 --output daily_short.mp4
```

| Flag | Purpose |
| --- | --- |
| `--topic` | Skip the AI topic picker and use your own topic. |
| `--runs N` | Generate N videos in one invocation (default `1`). |
| `--lang` | Script and voiceover language: `en-GB` (default) or `en-US`. |
| `--voice` | A Gemini voice ID (default `en-gb-tutor-13`, a UK female narrator, or `en-us-storyteller-14` with `--lang en-US`) or an `edge-tts` voice ending in `Neural`. |
| `--output` | Output filename for a single run (default: timestamped). |
| `--keep-cache` | Keep intermediate audio/video files instead of cleaning them. |
| `--fail-fast` | Stop a batch at the first failed run. |
| `--check` | Run preflight checks (ffmpeg, API keys, avatar) and exit. |
| `--upload` | Upload each finished video to YouTube (see below). |
| `--privacy` | `private` (default), `unlisted`, or `public` for uploads. |
| `--avatar-clips` | `auto` (default: hook + 3-5 scenes, more where the stock footage fits badly), `3` (hook + two middle scenes), `2` (two middle scenes only) or `0` (no avatar). Overrides `AVATAR_CLIPS`. |

Schedule a daily short with cron:

```cron
0 9 * * * cd /path/to/AutoShorts-AI && /usr/bin/python3 main.py >> run.log 2>&1
```

---

## Web Interface

`webapp.py` is the control panel for the whole pipeline, on the desktop or your
phone: every option (topic, number of videos, language, voice, avatar layout and
style, YouTube upload and privacy, advanced flags) with a hint under each, a
"Check setup" button, a live log, ComfyUI status, recent API errors, and a
carousel of the videos in `0_output` (Ready) and `1_uploaded` (Uploaded). Each
ready video has **Upload** and **Delete**; **Upload all** sends every ready video
(uploads use the privacy picked under Publish, public by default). On a desktop at least 1280px
wide the options, the run card (Generate, ComfyUI, progress, log) and the
portrait videos sit in three columns on one screen.

Before a run, the panel estimates how long it will take from the options you have
set, and updates as you change them: videos, avatar layout and source, clips per
video, clip length and uploading. During a run it shows an ETA for each step and for
the whole run, including any videos still to come. It keeps the original ETA from the
start of the run next to an adjusted one, and lists what moved it: a clip that failed its
check and renders again, more or fewer clips than expected, or steps running slower or
faster than estimated. Finished steps are timed and saved to `logs/stage_timings.json`, so
the estimates start rough and get better with each run. Delete that file to start over.

The left column lists today's quota per model: Gemini text and voice models, the stock
provider and YouTube upload units. Google has no API for what is left of a quota, so
Gemini and YouTube are counted from this app's own successful calls
(`logs/api_usage.jsonl`), and a daily-limit error from Google marks a model as used up,
with the limit it states replacing the free-tier figure, until a later call to that model
gets through. Calls from other apps on the same key aren't counted. Stock figures come from
the provider's response headers.

Gemini and YouTube counts, limit-hit marks and the panel's API error list all start over at
midnight Pacific, when Google resets its quotas, whatever the local time is. A batch
running past that moment tries its used-up models again. The log files keep the full history.

Uploads from the panel run `publish.py`, which you can also use directly:
`python publish.py short_20261029_233100.mp4 --privacy unlisted` or
`python publish.py --all`. They can run while a video renders, with their own log
above the carousel; only one upload runs at a time. Gemini writes the title,
description, hashtags and tags from the video's recorded topic and narration; a
video made before the topic history existed needs `--topic` (the panel asks for it).

While a run or upload is going you can queue more. **Generate** becomes **Add to
queue**, and the Upload buttons become **Queue upload**. Runs and uploads have a queue
each, so uploads still go during a render. A queued job keeps the options it was
queued with, whatever you change on the form afterwards, and **Upload all** means the
videos in Ready when you pressed it. Jobs start in order; one that fails doesn't stop
the rest, and the queue lists how each one ended. A video deleted or uploaded while
waiting is skipped. **Stop** puts what is queued on hold until you press **Resume**, and
✕ takes a job out of the queue. Adding or removing a job moves the running ETA, which
lists each queued job among what moved it; jobs on hold don't count. The queue lives in the
server's memory: Shut down is refused while anything is queued, and a server crash loses it.

Every successfully created video records its topic, narration and filename in
`logs/generated_topics.jsonl`. Before a script is written, a fast Gemini model
(`GEMINI_FAST_MODEL`) checks the new topic against that history and rejects it
if a viewer would feel they had already seen that video (a reworded repeat
counts; another fact in the same broad subject does not). The AI picker then
suggests another topic (up to 3 tries); a topic you typed in yourself stops the
run with a message. This applies to `main.py` and the daily pipeline too.

### Windows: one click

Double-click **`start_webapp.pyw`** in the project folder. It starts the panel's
server in the background (no console window; its output goes to
`logs/webapp.log`) and opens the panel in your default browser. If the server
is already running it just opens the browser. Stop the server with **Shut down**
at the top of the panel.

For a Desktop / Start menu shortcut that does the same, run once:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\create_shortcut.ps1
```

The server listens on your home network, so the panel also shows the address
to open on your phone. When a generation uses ComfyUI avatar clips, it starts
ComfyUI automatically if `COMFYUI_LAUNCHER` (e.g.
`C:\ComfyUI\run_nvidia_gpu.bat`) is set in `.env`, and the avatar stage waits up
to 3 minutes for it to finish loading. It runs the script's `main.py` command
with `--disable-auto-launch`, so no ComfyUI browser tab opens, in a minimised
console window you can close to stop it. **Stop** (next to Add to queue while a
run is going) kills the run and its ffmpeg renders and cancels the ComfyUI clip
it was rendering; the panel and ComfyUI keep running. Without a launcher, start ComfyUI
yourself; failed avatar renders fall back to stock footage.

To keep that phone address the same, give the PC a fixed IP: in your router's
admin page, add a **DHCP reservation** for the PC's MAC address (Windows:
`Get-NetAdapter`). `http://<pc-name>.local:8800` also works on most phones.
The first time it starts, allow Python through Windows Firewall on **private**
networks.

### Manually

```bash
pip install -r requirements.txt

# Local only
python webapp.py

# Reachable from your phone on the same network
APP_PASSWORD=pick-something python webapp.py --host 0.0.0.0 --port 8800
```

Then open `http://<your-computer-ip>:8800` on the phone.

- One run at a time — rendering saturates the CPU, so a second concurrent run
  would only make both slower. Starting one while another is going queues it.
- **`--host` other than `127.0.0.1` requires `APP_PASSWORD`.** The server
  refuses to start otherwise: anyone who found the URL could spend your Gemini
  and stock video quota.
- Set `FLASK_SECRET_KEY` too, otherwise the signing key is regenerated on every
  restart and you get logged out.

### Docker

```bash
cp .env.example .env      # fill in the keys, APP_PASSWORD and FLASK_SECRET_KEY
docker compose up -d
```

The image ships its own ffmpeg and serves the app with gunicorn. A few things
are deliberate:

- The port is published on `127.0.0.1` only. Put Caddy, nginx, or a Cloudflare
  Tunnel in front rather than exposing the container directly.
- **One worker.** The job state that tracks a running render lives in process
  memory, so a second worker would report "idle" while a render is going.
- `0_output` and `1_uploaded` are volumes, so finished videos survive a rebuild, and
  `assets/avatar` is mounted read-only.
- Compose refuses to start without `APP_PASSWORD` and `FLASK_SECRET_KEY` set.

Note that a container is not where you want a *scheduled* run — for that use the
GitHub Actions workflow or cron on the host, both of which call `main.py`
directly.

To reach it from outside your network, put it behind a tunnel
(`cloudflared tunnel --url http://localhost:8000`, `ngrok http 8000`) or a
reverse proxy with HTTPS — with `APP_PASSWORD` set, since the app has no other
protection.

---

## Claude Code on the web

`.claude/hooks/session-start.sh` prepares a remote session automatically:
installs ffmpeg, installs the Python dependencies, and appends the agent
proxy's CA to certifi — `edge-tts` pins certifi's bundle directly, so without
that last step every voiceover fails TLS verification and no audio is produced.

The hook only runs when `CLAUDE_CODE_REMOTE=true`, so local machines are left
alone. Put `GEMINI_API_KEY` and your stock video key (`PIXABAY_API_KEY` or `PEXELS_API_KEY`, matching `VIDEO_PROVIDER`) in the environment config and a
session comes up ready to run `python main.py`.

---

## Auto-Upload to YouTube

With `--upload`, the pipeline writes the title, description and tags with Gemini
and publishes the finished short straight to your channel. Uploading needs OAuth,
not just an API key, so there is a one-time browser step.

### 1. Create OAuth credentials

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project.
2. Enable **YouTube Data API v3**.
3. **APIs & Services → Credentials → Create OAuth client ID → Desktop app**.
4. Copy the client ID and client secret.

### 2. Mint a refresh token (once, on a machine with a browser)

```bash
python scripts/authorize_youtube.py --client-id XXX --client-secret YYY
```

It opens the Google consent screen and prints three values. Put them in your
`.env` for local runs, or add them as repository secrets (`YOUTUBE_CLIENT_ID`,
`YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN`) for the Actions workflow.

### 3. Run

```bash
python main.py --upload                      # publishes as private
python main.py --upload --privacy unlisted
python main.py --runs 3 --upload             # batch, each one uploaded
```

### Things that will bite you

- **Uploads default to `private`.** An unverified Google Cloud project has its
  uploads forced private by YouTube regardless of what you pass, so treat
  `--privacy public` as taking effect only after your app is verified.
- **A project in "Testing" mode expires its refresh token after 7 days.** Move
  the OAuth consent screen from *Testing* to *In production* to get a
  long-lived token, otherwise the scheduled workflow starts failing weekly.
- **Quota.** An upload costs 1600 units and its thumbnail 50, against a default
  daily quota of 10,000, so roughly **6 uploads per day** before the API starts
  refusing.
- **Thumbnails always show the avatar.** A run saves the frame of its accepted avatar
  clips that best faces the camera, or a crop of the reference image if it has none, as
  `<video>.still.jpg`. On upload a Gemini image model (`GEMINI_IMAGE_MODEL`, default
  `gemini-3.1-flash-image,gemini-2.5-flash-image`) builds the thumbnail from it, with her
  face as the focus and a scene and short text from the video behind and above her. An
  image where her face can't be seen or is too small is tried once more. If Gemini can't
  make one, the still itself is used. With no avatar image at all, no thumbnail is set.
  Google gives its image models no free-tier quota, so the Gemini step needs billing on
  the API key; without it every thumbnail is the plain still.
- **Thumbnails need a verified channel.** Without
  [phone verification](https://www.youtube.com/verify) YouTube refuses them with a 403;
  the video stays up and the log says so.
- Every successful upload moves the MP4 from `0_output/` to `1_uploaded/`, so
  `0_output/` only ever holds videos still waiting to go up.
- A failed upload never discards the video — the MP4 stays in `0_output/`
  and the workflow still publishes it as an artifact.

---

## Module Breakdown

### `brain.py` ( The Writer)

- **Input:** Topic string.
- **Logic:** Prompts Gemini to create an 8-9 scene JSON script. It asks for **two** visual keywords per scene (`visual_1`, `visual_2`) to enable the A/B split effect.

### `audio.py` (The Voice)

- **Input:** Text script.
- **Logic:** Generates a WAV clip per scene with Gemini TTS, using the scene's `mood` as the delivery style. The whole script is voiced in **one request** and cut into scenes at the pauses between them, so a video costs 1 TTS request instead of ~9 (the free tier allows 10/day per model). If the cut looks wrong it falls back to one request per scene, paced to the per-minute limit. If a model fails, hits its daily quota or doesn't answer within `GEMINI_TTS_TIMEOUT`, the whole video is re-voiced with the next model in the chain so takes never mix; if all fail, the run fails. Every API error is printed with the server's message and appended to `logs/api_errors.log`.
- **Post-Processing:** Reads durations with `mutagen` so scenes can be synced to audio length.

### `asset_manager.py` (The Librarian)

- **Input:** Visual keywords.
- **Logic:** Searches the provider set by `VIDEO_PROVIDER` (`pixabay` or `pexels`) for **Portrait (9:16)** videos. Pexels filters by orientation server-side; Pixabay has no orientation filter, so portrait clips are picked from the results and landscape ones are a center-cropped fallback. Downloads pairs of videos for every scene, and uses each clip only once per video. Handles fallbacks (if Video B is missing, Video A carries the scene). Search responses are cached for 24h in `assets/search_cache/<provider>/`, as Pixabay's API terms require.

### `composer.py` (The Editor)

- **Input:** Audio files + Video files.
- **Logic:**
- **Scene Processing:** Cuts the scene duration in half. Plays Video A for the first half, Video B for the second half. Stock clips never loop: a clip that ends early leaves its time to the other, one clip plays straight through, and clips too short between them are slowed down to fit.
- **Avatar Injection:** Puts the Avatar on the scenes chosen by the avatar plan (see *Generated Avatar Clips*), replacing the stock footage with your Avatar loop, or with lip-synced clips from `avatar_generator.py` when `AVATAR_SOURCE=comfyui`.
- **Stitching:** Merges all scenes using `xfade` transitions (wipes, slides).
- **Rendering:** Exports as `yuv420p` H.264 MP4 with `faststart` flags for maximum compatibility.

---

## Troubleshooting

**Q: The video is black or corrupt (0x80004005 error).**

- **Fix:** This is usually a Windows codec issue. The updated `composer.py` forces `pix_fmt='yuv420p'`. Try opening the file with VLC Media Player.

**Q: "Avatar file missing" error.**

- **Fix:** Ensure your folder structure is exactly `assets/avatar/avatars.mp4`.

**Q: The audio is silent or fails.**

- **Fix:** Check your internet connection and that `edge-tts` is installed from `requirements.txt`.

**Q: FFmpeg error "Exec format error" or "not found".**

- **Fix:** Ensure FFmpeg is installed and accessible from your command line.

---

## License

This project is open-source. Feel free to modify and build your own automation empire!
