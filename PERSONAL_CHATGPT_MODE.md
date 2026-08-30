# Personal ChatGPT Mode

This is the recommended Tokisclone architecture while the tool has one owner.

## Architecture

```text
You
 ↓
ChatGPT
 ↓
Google Drive / Tokisclone / Queue
 ↓
Tokisclone local worker on your computer
 ↓
TikTok through your normal internet connection
 ↓
Google Drive archive
```

ChatGPT is the interface. Google Drive is both the lightweight job queue and the permanent media library. Your computer supplies the TikTok-facing network connection and temporary processing compute.

No dashboard, database, public MCP server, GitHub-hosted media worker, or TokScript subscription is required for the personal version.

## Why this is now the default

We tested several GitHub-hosted approaches against a real public TikTok profile:

- yt-dlp profile discovery
- yt-dlp with browser TLS impersonation
- TikTok public profile hydration
- TikTok's guest post-list request
- a real headless Chrome session

The public profile itself loaded, but TikTok's video-list request returned `403` from GitHub's datacenter network. That makes GitHub-hosted Actions a poor default for TikTok extraction even when the code is otherwise correct.

Running the media worker on the owner's normal network removes that datacenter-origin problem while keeping the workflow simple.

## Drive folders

The current Tokisclone root contains:

```text
Tokisclone/
├── Queue/
├── Done/
├── Failed/
├── TikTok/
├── Instagram/
├── YouTube/
└── exports/
```

The local worker creates creator-specific `videos`, `metadata`, and `transcripts` folders when needed.

## How a request works

1. You tell ChatGPT what to save or sync.
2. ChatGPT creates a small JSON instruction and uploads it to `Tokisclone/Queue` in the connected Google Drive.
3. `local_worker.py`, running on your computer, reads the queued instruction.
4. The worker discovers/downloads the public TikTok content over your normal internet connection.
5. It checks Drive for duplicate video IDs.
6. New media, metadata, and optional transcripts are saved directly into Google Drive.
7. The worker writes a result JSON and moves the request to `Done` or `Failed`.
8. ChatGPT can inspect those folders and report the result to you.

## Supported local jobs

### Save one video

```json
{
  "kind": "video",
  "platform": "tiktok",
  "url": "https://www.tiktok.com/@creator/video/1234567890",
  "transcribe": false
}
```

### Sync a public creator

```json
{
  "kind": "profile",
  "platform": "tiktok",
  "profile_url": "https://www.tiktok.com/@creator",
  "limit": 100,
  "download_new_only": true,
  "transcribe": false
}
```

### Diagnostic

```json
{
  "kind": "diagnostic",
  "platform": "tiktok",
  "message": "Tokisclone OK"
}
```

## Local setup

See [`LOCAL_WORKER_SETUP.md`](LOCAL_WORKER_SETUP.md).

The one-time local requirements are:

- Python 3.12+
- this repository cloned locally
- a Google OAuth Desktop client for your own Drive account
- `client_secret.json` kept locally
- `python authorize_drive.py` once
- `python run_local_worker.py` whenever you want the queue processed

The ChatGPT Google Drive connection and the local program's Google authorization are separate. The local worker cannot borrow ChatGPT's private Drive token.

## Optional direct MCP mode

The repository still contains the MCP server implementation for a future always-on deployment. That is not needed for the personal version.

## GitHub Actions

The GitHub-hosted media workflows remain useful as diagnostics and for services that do not reject datacenter traffic. They should not be treated as the primary TikTok backend.

## Security

Make the repository private before treating it as a personal operational project.

Never commit or upload:

- `token.json`
- `client_secret.json`
- `.env`
- `cookies.txt`
- private media
- access credentials

The local worker does not require an inbound port and should not be exposed publicly.

## Access boundary

Tokisclone is for public content the owner is authorized to archive or use. It does not implement CAPTCHA bypassing, private-account access, DRM circumvention, credential theft, or other access-control bypasses.
