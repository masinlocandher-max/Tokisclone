# Tokisclone

Tokisclone is a clean-room personal media archiving tool for public TikTok videos and public DramaFren episodes, with bulk processing, Google Drive storage, metadata, retries, deduplication, and optional TikTok transcription.

It does not use TokScript code and does not depend on TokScript.

## Product rules

### TikTok

Two rules are mandatory and are not user-configurable:

1. **Clean-only source selection.** Tokisclone never intentionally selects TikTok's known marked `download` rendition. There is no `prefer-clean` or `allow` mode.
2. **One profile URL means all public videos.** Profile jobs do not use a video limit. Tokisclone asks the active extractor to enumerate every public video it can see for that profile, deduplicates the results, and processes the complete discovered set.

Legacy TikTok job fields such as `watermark_policy`, `limit`, or `max_items` may still exist in old queued JSON for compatibility, but they cannot relax these rules.

### DramaFren

DramaFren follows a parallel all-content rule:

1. **One DramaFren title or watch URL means all public listed episodes for that drama.** Tokisclone extracts the drama `id`, normalizes the URL to the public detail page, reads the complete public episode list, then processes the full set.
2. **Public direct video or unencrypted HLS only.** The resolver refuses encrypted HLS and does not bypass DRM, authentication, paywalls, CAPTCHA, or access controls.
3. **Human verification stays human.** If the ordinary DramaFren page asks for Cloudflare verification, Tokisclone opens its persistent Chrome profile and waits for the owner to complete the verification manually. It never solves or clicks the challenge automatically.

Tokisclone does not blur, crop, paint over, or digitally erase burned-in watermarks.

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
TikTok or DramaFren public content
 ↓
Google Drive permanent archive
 ↓
Done / Failed result + manifest
```

ChatGPT can be the conversational interface. Google Drive is the queue and permanent library. Your computer provides the temporary media-processing compute, so an always-on paid server is not required for the personal setup.

GitHub remains the source repository and CI system. GitHub-hosted runners are a secondary processing route because consumer media sites can behave differently on datacenter IPs.

## Supported personal queue jobs

- `video` — save one public TikTok video
- `profile` / `bulk_profile` — discover and save all public videos from one TikTok profile
- `bulk_urls` — save an explicit list of TikTok video URLs
- `dramafren` — take one DramaFren detail/watch URL and archive every public listed episode
- `diagnostic` — verify the Drive queue

Bulk jobs can partially succeed. Successful items remain archived while failures are reported in the result/manifest.

## Google Drive behavior

TikTok uses searchable platform/video IDs and skips already stored videos where possible.

DramaFren uses the existing DramaFren library structure and checks for `Episode NNN.mp4` under the title folder before downloading, so re-running the same drama skips episodes already archived.

Typical structure:

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
├── Library/
│   └── DramaFren/
│       └── Drama Title/
│           ├── Episode 001.mp4
│           ├── Episode 002.mp4
│           └── ...
├── Manifests/
└── exports/
```

## One-time local setup

Requirements:

- Python 3.12+
- Git
- FFmpeg
- Google account
- Google Cloud OAuth Desktop client with Google Drive API access
- Chromium for DramaFren browser-assisted resolution

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

Install Chromium once for DramaFren:

```bash
python -m playwright install chromium
```

Copy `.env.example` to `.env`, set `GOOGLE_DRIVE_ROOT_FOLDER_ID`, then authorize Google Drive once:

```bash
python authorize_drive.py
```

The generated `token.json` stays local and is excluded from Git.

Start the unified worker:

```bash
python local_worker.py
```

## Queue examples

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

### One DramaFren URL, all public listed episodes

```json
{
  "kind": "dramafren",
  "platform": "dramafren",
  "url": "https://dramabox.dramafren.org/index.php?id=42000005228&lang=en&view=detail",
  "retry_failed_once": true
}
```

A DramaFren watch URL works too:

```json
{
  "kind": "dramafren",
  "platform": "dramafren",
  "url": "https://dramabox.dramafren.org/index.php?ep=12&id=42000005228&lang=en&view=watch"
}
```

Tokisclone strips the episode-specific part, resolves the drama ID, discovers the public title page, and processes the complete public listed episode set.

Put the JSON in `Tokisclone/Queue`. The worker moves completed requests and their `.result.json` files to `Done`; fatal job errors go to `Failed`.

## DramaFren browser profile

DramaFren uses a persistent local browser profile by default:

```text
~/.tokisclone/dramafren-browser
```

Override it in `.env`:

```text
DRAMAFREN_BROWSER_PROFILE=/path/to/profile
```

The persistent profile allows the normal browser session to retain site state. If Cloudflare requests human verification, complete it manually in the opened Chrome window. The worker resumes after the real page appears.

## DramaFren download boundary

The existing DramaFren resolver:

- observes media requests from the normal public page
- prefers direct MP4
- supports unencrypted HLS
- rejects HLS manifests containing `#EXT-X-KEY`
- does not decrypt protected streams
- does not bypass CAPTCHA, paywalls, authentication, DRM, or access controls

“One drama URL = all episodes” means all episodes publicly listed by the detail page and successfully exposed to the current normal browser session.

## Optional TikTok transcription

Install:

```bash
pip install faster-whisper
```

Then request `"transcribe": true` on a supported TikTok job. The default local model is `small`; configure `TOKISCLONE_WHISPER_MODEL` in `.env` if needed.

## Optional TikTok cookies

If TikTok requires your normal logged-in session for content you are authorized to access, Tokisclone can use your own exported Netscape-format cookies:

```text
TOKISCLONE_COOKIE_FILE=cookies.txt
```

Keep that file local.

## Other DramaFren execution paths

The repository also contains dedicated DramaFren utilities/workflows for diagnostics and packaging, including:

- `dramafren_drive_worker.py`
- `dramafren_bulk.py`
- `.github/workflows/dramafren-download.yml`
- `.github/workflows/package-drama-local-job.yml`

For normal personal use, the unified `local_worker.py` + Google Drive `Queue` is the intended interface.

## GitHub Actions bridge for TikTok

`process_job.py` and `.github/workflows/process-jobs.yml` support TikTok jobs such as:

- `video`
- `profile`
- `bulk_profile`
- `bulk_urls`
- `inspect`
- `diagnostic`

Both `profile` and `bulk_profile` mean all public videos discoverable by the extractor.

## MCP mode

The repository includes the remote MCP implementation for the existing Tokisclone server. The personal Drive-queue mode does not require MCP hosting.

## Reliability

TikTok, DramaFren, Cloudflare, browser behavior, and extractor behavior can change. Keep dependencies current and expect site-specific maintenance over time.

For TikTok, “all public videos” means all public entries the active extractor/current session can enumerate.

For DramaFren, “all public listed episodes” means every episode listed on the public drama page that the current normal browser session can resolve as direct video or unencrypted HLS.

## Security

Never commit:

- `.env`
- `token.json`
- `client_secret.json`
- `credentials.json`
- `cookies.txt`
- browser profile contents
- private media

Making the repository private is recommended for a personal deployment.

## Rights

Only download, archive, transcribe, or reuse media you own, have permission to use, or are otherwise legally entitled to process.
