# Tokisclone

Tokisclone is a clean-room personal tool for public TikTok archiving and public DramaFren drama archiving, with bulk downloads, metadata capture, optional transcription, and Google Drive storage.

It does not use TokScript code and does not depend on TokScript.

## Product rules

For TikTok, two rules are mandatory and are not user-configurable:

1. **Clean-only source selection.** Tokisclone never intentionally selects TikTok's known marked `download` rendition. There is no `prefer-clean` or `allow` mode.
2. **One profile URL means all public videos.** Profile jobs do not use a video limit. Tokisclone asks the active extractor to enumerate every public video it can see for that profile, deduplicates the results, and processes the complete discovered set.

For DramaFren:

1. **One drama URL means all public episodes.** A detail or watch URL is normalized to its drama ID and public episode list.
2. **Direct public non-DRM media only.** Tokisclone processes media exposed by the public player or an authorized user session and rejects media that appears DRM-protected.

Tokisclone does not blur, crop, paint over, or digitally erase burned-in watermarks. It does not bypass private access, paywalls, authentication, CAPTCHA, DRM, or license servers.

See [`BULK_MVP.md`](BULK_MVP.md) for TikTok bulk behavior and [`DRAMAFREN.md`](DRAMAFREN.md) for the DramaFren adapter.

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
TikTok public content or DramaFren public player
 ↓
Google Drive / Tokisclone / <platform> / <creator or drama>
 ↓
Done or Failed result
```

ChatGPT can be the conversational interface. Google Drive is the queue and permanent library. Your own computer provides temporary media-processing compute, so an always-on paid server is not required for the personal setup.

GitHub remains the source repository and CI system. GitHub-hosted runners are a secondary processing route because consumer media sites can behave differently on datacenter IPs.

## What the worker does

Supported personal jobs:

- `video` — save one public TikTok video
- `profile` / `bulk_profile` — discover and save all public videos from one TikTok profile
- `bulk_urls` — save an explicit list of TikTok video URLs
- `dramafren` — discover and archive all public episodes from one DramaFren title/watch URL
- `diagnostic` — verify the Drive queue

For TikTok, existing Drive video IDs are skipped before download when possible.

For DramaFren, the dedupe key is `drama_id:episode`, so re-running the same drama skips episodes already archived in Drive.

Bulk jobs can partially succeed. Successful items remain saved while failed items are reported in the manifest.

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
├── Dramafren/
│   └── Drama Title/
│       ├── videos/
│       └── metadata/
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

### One TikTok video

```json
{
  "kind": "video",
  "platform": "tiktok",
  "url": "https://www.tiktok.com/@creator/video/123456789",
  "transcribe": false
}
```

### One TikTok profile, all public videos

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

### Explicit TikTok URL batch

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

### One DramaFren URL, all public episodes

```json
{
  "kind": "dramafren",
  "platform": "dramafren",
  "url": "https://dramabox.dramafren.org/index.php?id=42000005228&lang=en&view=detail",
  "retry_failed_once": true
}
```

A DramaFren watch URL with the same `id` also works. Tokisclone uses the drama ID to load the public detail page and enumerate the full public episode list.

Put job JSON in `Tokisclone/Queue`. The worker moves completed requests and their `.result.json` files to `Done`; fatal job errors go to `Failed`.

## Optional transcription

Install:

```bash
pip install faster-whisper
```

Then request `"transcribe": true` on supported TikTok jobs. DramaFren transcription is not enabled by the adapter yet; the priority is reliable full-drama archiving first.

The default local model is `small`; change `TOKISCLONE_WHISPER_MODEL` in `.env` if needed.

## Optional session cookies

If a supported site requires your normal logged-in session for content you are authorized to access, Tokisclone can use your own exported Netscape-format cookies:

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
- `dramafren`
- `inspect`
- `diagnostic`

A DramaFren Actions job writes `inventory.json`, an `episodes/` directory, `manifest.json`, and `failures.json` into the workflow artifact. The local Drive worker remains the preferred route when the site restricts datacenter requests.

## MCP mode

The repository includes MCP tools for TikTok plus:

- `inspect_dramafren_drama(url)`
- `inspect_dramafren_episode(url)`
- `archive_dramafren_to_drive(url)`

`archive_dramafren_to_drive` discovers the complete public episode list, skips already archived `drama_id:episode` keys, downloads direct/non-DRM media, retries failures once, and stores results plus metadata in Google Drive.

## Reliability

Tokisclone uses yt-dlp for best-effort public extraction and its own site adapter for DramaFren episode enumeration. Sites can change markup, player behavior, login requirements, or anti-automation behavior.

Keep yt-dlp current:

```bash
pip install -U yt-dlp
```

For TikTok, "all public videos" means all public entries the active extractor and current session can successfully enumerate.

For DramaFren, "all public episodes" means all episodes listed by the public detail page and successfully exposed to the current player/session. An indexed DramaFren detail page can explicitly publish totals and episode lists, such as a page showing `Total: 64 Eps` with Ep 1 through Ep 64.

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
