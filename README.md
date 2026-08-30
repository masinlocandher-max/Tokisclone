# Tokisclone

Tokisclone is a clean-room personal tool for public TikTok video archiving, bulk downloads, metadata capture, optional transcription, and Google Drive storage.

It is not TokScript code and does not depend on TokScript.

## MVP status

The bulk-download MVP is implemented.

Tokisclone can now take one creator/profile request or a list of video URLs, discover/deduplicate the batch, prefer a cleaner source rendition when the extractor exposes one, retry failures once, and produce a manifest. The personal worker can archive results directly to Google Drive.

See [`BULK_MVP.md`](BULK_MVP.md) for job schemas and operational details.

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
- `profile` / `bulk_profile` — discover a public TikTok profile and save new videos
- `bulk_urls` — save many explicit TikTok URLs in one request
- `diagnostic` — verify the Drive queue

For each saved video, Tokisclone stores searchable Drive metadata including platform, creator, TikTok video ID, source URL, and source-selection policy. Duplicate video IDs are skipped before download when possible.

A bulk sync can partially succeed. If 99 videos save and one fails, the job still reports the 99 successes and the failed item instead of discarding completed work.

## Cleaner-source selection

The default is:

```text
TOKISCLONE_WATERMARK_POLICY=prefer-clean
```

Policies:

- `prefer-clean` — prefer formats excluding TikTok's known marked `download` rendition, then use a generic fallback for compatibility.
- `clean-only` — do not intentionally select that known marked rendition.
- `allow` — normal best-format selection.

Tokisclone does not blur, crop, or digitally erase watermarks. A genuinely clean result depends on what source renditions the platform exposes to the current session/region.

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
│       └── transcripts/
├── Instagram/
├── YouTube/
└── exports/
```

## One-time local setup

Requirements:

- Python 3.12+
- Git
- your Google account
- Google Cloud OAuth Desktop client for Google Drive
- FFmpeg recommended for media merging/conversion

Clone:

```bash
git clone https://github.com/masinlocandher-max/Tokisclone.git
cd Tokisclone
python -m venv .venv
```

Activate:

```bash
source .venv/bin/activate
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Install the worker:

```bash
pip install -r requirements-worker.txt
```

### Google Drive authorization

1. Enable Google Drive API in your Google Cloud project.
2. Configure OAuth consent for your own account.
3. Create an OAuth Client ID of type **Desktop app**.
4. Save the downloaded JSON locally as `client_secret.json`.
5. Copy `.env.example` to `.env`.
6. Set `GOOGLE_DRIVE_ROOT_FOLDER_ID`.
7. Run:

```bash
python authorize_drive.py
```

The generated `token.json` stays local and is excluded from Git.

Start:

```bash
python local_worker.py
```

## Job examples

Single video:

```json
{
  "kind": "video",
  "platform": "tiktok",
  "url": "https://www.tiktok.com/@creator/video/123456789",
  "watermark_policy": "prefer-clean",
  "transcribe": false
}
```

Bulk profile:

```json
{
  "kind": "bulk_profile",
  "platform": "tiktok",
  "profile_url": "https://www.tiktok.com/@creator",
  "limit": 200,
  "download_new_only": true,
  "watermark_policy": "prefer-clean",
  "retry_failed_once": true,
  "transcribe": false
}
```

Bulk URL list:

```json
{
  "kind": "bulk_urls",
  "platform": "tiktok",
  "urls": [
    "https://www.tiktok.com/@creator/video/123456789"
  ],
  "watermark_policy": "prefer-clean",
  "retry_failed_once": true
}
```

Put the JSON in `Tokisclone/Queue`. The worker moves completed requests and a `.result.json` file to `Done`. Fatal errors go to `Failed`.

## Optional transcription

Install:

```bash
pip install faster-whisper
```

Then set:

```json
{
  "transcribe": true
}
```

The default model is `small`; configure `TOKISCLONE_WHISPER_MODEL` in `.env`.

## Optional TikTok cookies

If a normal logged-in session is required, Tokisclone can use your own exported Netscape-format cookies:

```text
TOKISCLONE_COOKIE_FILE=cookies.txt
```

Keep that file local. Tokisclone does not implement private-account access, CAPTCHA bypass, DRM circumvention, credential theft, or anti-bot bypass.

## GitHub Actions bridge

`process_job.py` and `.github/workflows/process-jobs.yml` can now execute:

- `video`
- `profile` (legacy inventory-only)
- `bulk_profile`
- `bulk_urls`
- `inspect`
- `diagnostic`

GitHub Actions artifacts are temporary. The local Drive worker remains the recommended permanent personal archive because consumer social platforms can behave differently on datacenter IPs.

## Optional MCP mode

The repository also contains the remote MCP implementation:

- `server.py`
- `drive_storage.py`
- `authorize_drive.py`
- `run_server.py`
- `Dockerfile`

The Drive-queue local mode does not require MCP hosting.

## Reliability

TikTok does not provide a general official API for enumerating arbitrary unrelated public accounts. Tokisclone uses yt-dlp for best-effort public extraction. TikTok and extractor behavior can change, so keep yt-dlp current:

```bash
pip install -U yt-dlp
```

If ordinary TikTok profile discovery fails, a `seed_video_url` from the same creator can be supplied so Tokisclone can attempt the channel-ID fallback.

## Cost model

Tokisclone has no TokScript subscription or per-video Tokisclone licensing fee.

In personal mode, practical costs are Google Drive storage, bandwidth, and your own computer time. Local transcription also consumes CPU/RAM.

## Security

Never commit:

- `.env`
- `token.json`
- `client_secret.json`
- `credentials.json`
- `cookies.txt`
- downloaded private media

Making the repository private is recommended for a personal deployment.

## Rights

Only download, archive, transcribe, or reuse media you own, have permission to use, or are otherwise legally entitled to process.
