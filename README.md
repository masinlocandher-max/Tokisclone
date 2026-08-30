# Tokisclone

A clean-room personal MCP server for public short-form video research, downloads, transcription, and Google Drive archiving.

Tokisclone is not TokScript code and does not connect to TokScript. It recreates the useful workflow with open tooling and your own Google Drive.

## Personal architecture

```text
You
 ↓
ChatGPT
 ↓
Tokisclone MCP
 ↓
Public video extraction / processing
 ↓
Temporary local file
 ↓
Google Drive
```

Google Drive is the permanent library. The MCP host only needs temporary disk space while processing a video.

## MCP tools

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

### Example requests from ChatGPT

- Save this TikTok to my Drive.
- Get the public videos from this creator.
- Sync this creator and save only videos I do not already have.
- Save this video and transcribe it.
- Show me what is already in my Tokisclone library.
- Find TikTok video ID 123456789 in my saved library.

## Drive organization

Tokisclone creates creator folders as needed beneath the configured root:

```text
Tokisclone/
├── TikTok/
│   └── creator/
│       ├── videos/
│       └── transcripts/
├── Instagram/
├── YouTube/
└── exports/
```

Saved media receives private Google Drive `appProperties` containing its platform, creator, source URL, video ID, and record type. Duplicate detection therefore does not require a separate database.

## Google Drive authorization

ChatGPT's own Google Drive connector credentials are not exposed to custom MCP servers. Tokisclone therefore needs one Google OAuth authorization for the account that owns the storage folder.

1. In Google Cloud, create or select a project.
2. Enable the Google Drive API.
3. Configure an OAuth consent screen for personal/testing use.
4. Create an OAuth Client ID of type **Desktop app**.
5. Download the credentials as `client_secret.json` into the project directory.
6. Install dependencies and run:

```bash
python authorize_drive.py
```

7. Approve access in the browser. Tokisclone writes `token.json` locally.
8. Never commit `client_secret.json` or `token.json`.

For a hosted deployment, put the complete contents of `token.json` into the secret `GOOGLE_DRIVE_TOKEN_JSON`.

Set the Drive root folder ID as:

```bash
GOOGLE_DRIVE_ROOT_FOLDER_ID=your_folder_id
```

See `.env.example`.

## Run locally

Requires Python 3.12+ and ffmpeg.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python server.py
```

The MCP Streamable HTTP endpoint is `/mcp` on the server's configured host/port.

For a lightweight install without local Whisper transcription:

```bash
pip install -r requirements-minimal.txt
```

## Docker

```bash
docker build -t tokisclone .
docker run --rm -p 8000:8000 \
  -e GOOGLE_DRIVE_ROOT_FOLDER_ID=YOUR_FOLDER_ID \
  -e GOOGLE_DRIVE_TOKEN_JSON='YOUR_TOKEN_JSON' \
  tokisclone
```

In real hosting, use the platform's encrypted secret manager rather than placing OAuth JSON directly in a shell command.

## How creator sync works

`sync_creator` performs a best-effort public profile discovery, checks each returned video ID against Drive metadata, downloads only missing videos, uploads them to Drive, and deletes temporary server copies.

The synchronous personal version is capped at 100 discovered items per call. Large libraries should eventually use a job queue.

## Important TikTok limitation

TikTok's official Display API is designed around authorized TikTok users and their own content. Arbitrary public-profile discovery cannot be implemented solely with that API.

Tokisclone currently uses `yt-dlp` for best-effort extraction of public content. Upstream site changes can temporarily break an extractor. Tokisclone deliberately does not include CAPTCHA bypassing, private-account access, DRM circumvention, credential theft, or anti-bot evasion.

## Costs

There is no TokScript subscription or per-video Tokisclone licensing charge. Your costs are primarily hosting compute, bandwidth, Google Drive storage, and optional transcription compute.

## Before exposing it publicly

This repository is intentionally optimized for one owner. Before turning it into a multi-user product, add MCP authentication, rate limits, per-user OAuth, quotas, a job queue, stronger URL/SSRF controls, retention rules, logging, and a real metadata database.

## Rights

Only download, archive, transcribe, or reuse media you own, have permission to use, or are otherwise legally entitled to process. Tokisclone does not attempt to access private content or bypass technical access controls.
