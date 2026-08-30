# Tokisclone

Tokisclone is a clean-room personal tool for public TikTok video archiving, metadata capture, optional transcription, and Google Drive storage.

It is not TokScript code and does not depend on TokScript.

## Recommended personal architecture

For one owner, the lowest-maintenance setup is:

```text
You
 ↓
ChatGPT
 ↓
Google Drive / Tokisclone / Queue
 ↓
local_worker.py on your own computer and normal internet connection
 ↓
TikTok public content
 ↓
Google Drive / Tokisclone / TikTok / <creator>
 ↓
Done or Failed result
```

ChatGPT is the conversational interface. Google Drive is both the job handoff and permanent library. Your own computer is the media worker, so no always-on paid server is required.

GitHub remains the source repository and CI system. GitHub-hosted runners are not the recommended TikTok extraction worker because TikTok can return different or degraded results to datacenter IPs.

## What the local worker does

`local_worker.py` watches the `Queue` folder inside your Tokisclone Google Drive folder.

Supported jobs:

- `video` — save one public TikTok video to Drive
- `profile` — discover a public TikTok profile and save new videos
- `diagnostic` — verify the Drive queue

For each saved video, Tokisclone stores searchable Drive metadata including platform, creator, TikTok video ID, and source URL. Duplicate video IDs are skipped.

A profile sync can partially succeed. If 99 videos save and one fails, the job is still completed and the result reports the failed item instead of discarding the successful work.

## Google Drive structure

The worker creates or uses this structure automatically:

```text
Tokisclone/
├── Queue/
├── Done/
├── Failed/
├── TikTok/
│   └── creator_name/
│       ├── videos/
│       ├── metadata/
│       └── transcripts/       # only when requested
├── Instagram/                 # reserved for future support
├── YouTube/                   # reserved for future support
└── exports/
```

## One-time local setup

Requirements:

- Python 3.12+
- Git
- your Google account
- a Google Cloud OAuth Desktop client for Google Drive

Clone the repository:

```bash
git clone https://github.com/masinlocandher-max/Tokisclone.git
cd Tokisclone
python -m venv .venv
```

Activate the environment.

macOS/Linux:

```bash
source .venv/bin/activate
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Install the lightweight worker dependencies:

```bash
pip install -r requirements-worker.txt
```

### Google Drive authorization

1. Create or use a Google Cloud project.
2. Enable the Google Drive API.
3. Configure the OAuth consent screen for your own Google account.
4. Create an OAuth Client ID of type **Desktop app**.
5. Download its JSON and save it locally in this repo as `client_secret.json`.

Do not commit that file. It is excluded by `.gitignore`.

Copy the environment template:

```bash
cp .env.example .env
```

On Windows, you can copy it in File Explorer or run:

```powershell
Copy-Item .env.example .env
```

Edit `.env` and set:

```text
GOOGLE_DRIVE_ROOT_FOLDER_ID=your_tokisclone_folder_id
```

Authorize once:

```bash
python authorize_drive.py
```

A browser window opens for your Google authorization. The resulting `token.json` stays on your computer and is excluded from Git.

Start the worker:

```bash
python local_worker.py
```

When running, it checks the Drive Queue every 15 seconds by default.

## Job examples

Single video:

```json
{
  "kind": "video",
  "platform": "tiktok",
  "url": "https://www.tiktok.com/@creator/video/123456789",
  "transcribe": false
}
```

Profile sync:

```json
{
  "kind": "profile",
  "platform": "tiktok",
  "profile_url": "https://www.tiktok.com/@creator",
  "limit": 200,
  "download_new_only": true,
  "transcribe": false
}
```

Put a job JSON file in `Tokisclone/Queue`. When the worker finishes, it moves the request plus a `.result.json` file to `Done`. Fatal errors go to `Failed`.

When ChatGPT has access to your connected Google Drive, the JSON handoff can be created for you from the conversation, so normal use can be as simple as asking to save a TikTok video or sync a profile.

## Optional transcription

The lightweight worker installation does not install Whisper. If you want local transcription:

```bash
pip install faster-whisper
```

Then submit jobs with:

```json
{
  "transcribe": true
}
```

The default local model is `small`. Change it in `.env` with `TOKISCLONE_WHISPER_MODEL`.

## Optional TikTok cookies

Public extraction normally does not require cookies. If TikTok asks for your normal logged-in session, Tokisclone can use your own exported Netscape-format `cookies.txt` file:

```text
TOKISCLONE_COOKIE_FILE=cookies.txt
```

Keep that file local. It is excluded from Git. Tokisclone does not attempt private-account access, CAPTCHA bypassing, DRM circumvention, credential theft, or anti-bot bypassing.

## GitHub Actions bridge

`process_job.py` and `.github/workflows/process-jobs.yml` remain as a lightweight diagnostic/temporary worker path. They are useful for CI and sources that tolerate datacenter traffic, but they are not the primary TikTok profile worker.

## Optional MCP mode

The repository also contains a remote MCP implementation for environments where direct custom MCP integration is appropriate:

- `server.py`
- `drive_storage.py`
- `authorize_drive.py`
- `run_server.py`
- `Dockerfile`

The Drive-queue local mode does not require MCP hosting.

## Reliability

TikTok does not provide a general official API for enumerating arbitrary unrelated public accounts. Tokisclone therefore uses yt-dlp for best-effort public extraction. TikTok and extractor behavior can change, so keep yt-dlp current:

```bash
pip install -U yt-dlp
```

If profile discovery works in your browser but fails in Tokisclone, update yt-dlp first. Your own exported cookies can be supplied when a normal logged-in session is required.

## Cost model

Tokisclone has no TokScript subscription and no per-video Tokisclone licensing fee.

In personal mode, your practical costs are your existing Google Drive storage, internet bandwidth, and your own computer time. Transcription also uses local CPU/RAM.

## Security

This repository contains no Google OAuth token or Drive credentials.

Never commit:

- `.env`
- `token.json`
- `client_secret.json`
- `credentials.json`
- `cookies.txt`
- downloaded private media

Because this is intended as a personal tool, making the repository private is recommended even though the secret files are excluded.

## Rights

Only download, archive, transcribe, or reuse media you own, have permission to use, or are otherwise legally entitled to process.
