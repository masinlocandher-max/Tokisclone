# Tokisclone

A clean-room personal tool for public short-form video research, downloads, transcription, and Google Drive archiving.

Tokisclone is not TokScript code and does not connect to TokScript. It recreates the useful workflow with open tooling and your own storage.

## Recommended personal mode

For a single owner, the simplest architecture is:

```text
You
 ↓
ChatGPT
 ↓
GitHub connector
 ↓
Tokisclone GitHub Action
 ↓
Temporary workflow artifact
 ↓
ChatGPT working runtime
 ↓
Google Drive connector
 ↓
Tokisclone library in Google Drive
```

This mode does **not** require an always-on server or a Google Drive refresh token stored in GitHub. Google Drive is the permanent library; GitHub Actions only supplies temporary processing compute.

See [`PERSONAL_CHATGPT_MODE.md`](PERSONAL_CHATGPT_MODE.md) for the full workflow.

## Personal job worker

`process_job.py` currently supports:

- `video` — process one public video URL
- `profile` — best-effort creator/profile inventory
- `diagnostic` — test the worker bridge

Jobs are submitted as JSON files under `jobs/`. `.github/workflows/process-jobs.yml` detects new job files, processes them with yt-dlp/ffmpeg, and exposes the output as a short-lived GitHub Actions artifact.

Example video job:

```json
{
  "kind": "video",
  "url": "https://...",
  "write_subtitles": false
}
```

Example profile inventory job:

```json
{
  "kind": "profile",
  "profile_url": "https://...",
  "limit": 100
}
```

## Google Drive library

The personal Drive structure starts with:

```text
Tokisclone/
├── TikTok/
├── Instagram/
├── YouTube/
└── exports/
```

Creator-specific folders can be added as the library grows.

## Optional direct MCP mode

The repository also contains a deployable MCP server for future direct integration:

- `server.py`
- `drive_storage.py`
- `authorize_drive.py`
- `run_server.py`
- `Dockerfile`

The MCP tools include:

- `inspect_video`
- `list_profile_videos`
- `save_video_to_drive`
- `sync_creator`
- `list_drive_library`
- `get_saved_video`
- `transcribe_video`
- `get_bulk_metadata`
- `export_records`
- `drive_status`
- `health`

Direct MCP mode uses Google OAuth for Drive because ordinary My Drive storage belongs to the human account. OAuth secrets are intentionally excluded from Git.

## Local MCP setup

Requires Python 3.12+ and ffmpeg.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python authorize_drive.py
python server.py
```

For a lightweight installation without local Whisper transcription:

```bash
pip install -r requirements-minimal.txt
```

## Docker

```bash
docker build -t tokisclone .
docker run --rm -p 8000:8000 \
  -e GOOGLE_DRIVE_ROOT_FOLDER_ID=YOUR_FOLDER_ID \
  -e GOOGLE_DRIVE_TOKEN_JSON='YOUR_TOKEN_JSON' \
  -e MCP_ALLOWED_HOSTS='your-host.example.com,your-host.example.com:*' \
  tokisclone
```

Use encrypted deployment secrets rather than placing OAuth JSON directly in a reusable shell command.

## Reliability note

TikTok's official Display API is designed around authorized TikTok users and their own content. Arbitrary public-profile discovery therefore cannot be implemented solely with the official API.

Tokisclone uses yt-dlp for best-effort public extraction. Upstream site changes can temporarily break an extractor, so the extraction layer should be treated as maintainable infrastructure rather than a one-time implementation.

## Costs

There is no TokScript subscription or per-video Tokisclone licensing fee. In personal bridge mode, costs are primarily whatever GitHub Actions and Google Drive usage exceeds the allowances of the user's existing accounts. Direct MCP mode can additionally incur hosting and transcription compute costs.

## Security

Before using real job URLs or personal workflow data, make the repository private.

Never commit:

- `token.json`
- `client_secret.json`
- `.env`
- private media files
- credentials or cookies

Tokisclone deliberately does not include private-account access, CAPTCHA bypassing, DRM circumvention, credential theft, or anti-bot evasion.

## Rights

Only download, archive, transcribe, or reuse media you own, have permission to use, or are otherwise legally entitled to process.
