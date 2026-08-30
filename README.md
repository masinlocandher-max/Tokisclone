# Tokisclone

Tokisclone is a clean-room personal tool for public TikTok video archiving, bulk downloads, metadata capture, optional transcription, and Google Drive storage.

It does not use TokScript code and does not depend on TokScript.

## Product rules

Two rules are now mandatory and are not user-configurable:

1. **Clean-only source selection.** Tokisclone never intentionally selects TikTok's known marked `download` rendition. There is no `prefer-clean` or `allow` mode anymore.
2. **One profile URL means all public videos.** Profile jobs do not use a video limit. Tokisclone asks the active extractor to enumerate every public video it can see for that profile, deduplicates the results, and processes the complete discovered set.

Legacy job fields such as `watermark_policy`, `limit`, or `max_items` may still exist in old queued JSON for compatibility, but they cannot relax these rules.

Tokisclone does not blur, crop, paint over, or digitally erase a watermark that is already burned into a file. If the available source is identified as watermarked, the strict downloader rejects it rather than pretending the output is clean.

See [`BULK_MVP.md`](BULK_MVP.md) for the bulk job flow.

## Recommended personal architecture

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

ChatGPT can be the conversational interface. Google Drive is the queue and permanent library. Your own computer provides temporary media-processing compute, so an always-on paid server is not required for the personal setup.

GitHub remains the source repository and CI system. GitHub-hosted runners are a secondary processing route because social platforms can behave differently on datacenter IPs.

## What the worker does

Supported personal jobs:

- `video` — save one public TikTok video
- `profile` / `bulk_profile` — discover and save all public videos from one TikTok profile
- `bulk_urls` — save an explicit list of TikTok video URLs
- `diagnostic` — verify the Drive queue

For every video Tokisclone can process, it can store the video, normalized metadata, searchable Drive properties, optional subtitles, and optional transcription. Existing Drive video IDs are skipped before download when possible.

A profile job can partially succeed. If 99 videos save and one fails, the 99 successes remain saved and the result reports the failed item.

## Google Drive structure

```text
Tokisclone/
├── Queue/
├── Done/
├── Failed/
├── TikTok/
│   └── creator_name/
│       ├── videos/
│       ├── metadata/
│       └── transcripts/
├── Instagram/
├── YouTube/
└── exports/
```

## One-time local setup

Requirements:

- Python 3.12+
- Git
- FFmpeg
- your Google account
- a Google Cloud OAuth Desktop client with Google Drive API access

Clone and create a virtual environment:

```bash
git clone https://github.com/masinlocandher-max/Tokisclone.git
cd Tokisclone
python -m venv .venv
```

macOS/Linux:

```bash
source .venv/bin/activate
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Install the personal worker dependencies:

```bash
pip install -r requirements-worker.txt
```

Copy `.env.example` to `.env`, set `GOOGLE_DRIVE_ROOT_FOLDER_ID`, then authorize Google Drive once:

```bash
python authorize_drive.py
```

The generated `token.json` stays local and is excluded from Git.

Start the worker:

```bash
python local_worker.py
```

## Job examples

### One video

```json
{
  "kind": "video",
  "platform": "tiktok",
  "url": "https://www.tiktok.com/@creator/video/123456789",
  "transcribe": false
}
```

### One profile, all public videos

```json
{
  "kind": "profile",
  "platform": "tiktok",
  "profile_url": "https://www.tiktok.com/@creator",
  "retry_failed_once": true,
  "transcribe": false
}
```

No `limit` is needed. `bulk_profile` is accepted as an alias for the same behavior.

### Explicit URL batch

```json
{
  "kind": "bulk_urls",
  "platform": "tiktok",
  "urls": [
    "https://www.tiktok.com/@creator/video/123456789",
    "https://www.tiktok.com/@creator/video/987654321"
  ],
  "retry_failed_once": true
}
```

Put the JSON in `Tokisclone/Queue`. The worker moves completed requests and their `.result.json` files to `Done`; fatal job errors go to `Failed`.

## Optional transcription

Install:

```bash
pip install faster-whisper
```

Then request:

```json
{
  "transcribe": true
}
```

The default local model is `small`; change `TOKISCLONE_WHISPER_MODEL` in `.env` if needed.

## Optional TikTok cookies

If TikTok requires your normal logged-in session for content you are authorized to access, Tokisclone can use your own exported Netscape-format cookies:

```text
TOKISCLONE_COOKIE_FILE=cookies.txt
```

Keep that file local. Tokisclone does not implement private-account bypass, CAPTCHA bypass, DRM circumvention, credential theft, or access-control evasion.

## GitHub Actions bridge

`process_job.py` and `.github/workflows/process-jobs.yml` support:

- `video`
- `profile`
- `bulk_profile`
- `bulk_urls`
- `inspect`
- `diagnostic`

For GitHub Actions, both `profile` and `bulk_profile` now mean: discover all public videos and download the full discovered set into the temporary workflow artifact.

## MCP mode

The repository also includes the remote MCP implementation:

- `server.py`
- `drive_storage.py`
- `authorize_drive.py`
- `run_server.py`
- `Dockerfile`

The MCP `list_profile_videos` and `sync_creator` tools no longer expose a profile limit. `sync_creator(profile_url)` attempts the complete public profile discovered by the extractor and uses the same mandatory clean-only downloader.

## Reliability

Tokisclone uses yt-dlp for best-effort public extraction. TikTok can change its web behavior, login requirements, format inventory, or anti-automation behavior. Keep yt-dlp current:

```bash
pip install -U yt-dlp
```

If ordinary profile discovery fails, a `seed_video_url` from the same creator can be supplied to the queue worker/Actions job so Tokisclone can attempt its channel-ID fallback.

"All public videos" means all public entries the active extractor and current session can successfully enumerate. It cannot include private, deleted, region-blocked, or otherwise unavailable videos.

## Security

Never commit:

- `.env`
- `token.json`
- `client_secret.json`
- `credentials.json`
- `cookies.txt`
- private media

Making the repository private is recommended for a personal deployment.

## Rights

Only download, archive, transcribe, or reuse media you own, have permission to use, or are otherwise legally entitled to process.
