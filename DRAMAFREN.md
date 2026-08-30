# DramaFren Adapter

Tokisclone can process one public DramaFren title/detail/watch URL as one full-drama job.

## Flow

```text
DramaFren URL
   ↓
parse drama id + language
   ↓
load public detail page
   ↓
discover complete public episode list
   ↓
try direct yt-dlp resolution
   ↓
if needed, load the ordinary public page in Chromium
and observe MP4 / HLS / DASH media requests
   ↓
reject apparent DRM-protected media
   ↓
download
   ↓
retry failures once
   ↓
inventory + manifest
   ↓
optional Google Drive archive
```

## Personal worker setup

Install the normal worker dependencies:

```bash
pip install -r requirements-worker.txt
```

Install the Chromium runtime once:

```bash
python -m playwright install chromium
```

The browser resolver is a fallback. Tokisclone tries normal direct extraction first.

## Job

```json
{
  "kind": "dramafren",
  "platform": "dramafren",
  "url": "https://dramabox.dramafren.org/index.php?id=42000005228&lang=en&view=detail",
  "retry_failed_once": true
}
```

A watch URL containing `id`, `lang`, and `ep` also works. Tokisclone normalizes it to the drama ID and processes the full public episode list.

## Google Drive behavior

The personal worker stores the drama under the `Dramafren` platform folder. Each episode uses a stable dedupe key:

```text
<drama_id>:<episode_number>
```

Re-running the same title therefore skips episodes already archived in Drive.

## Actions output

```text
inventory.json
episodes/
  0001/
  0002/
  ...
manifest.json
failures.json
```

## MCP tools

- `inspect_dramafren_drama(url)`
- `inspect_dramafren_episode(url)`
- `archive_dramafren_to_drive(url)`

## Safety boundary

Tokisclone only processes media exposed by the public player or an authorized normal user session. It does not bypass private access, paywalls, authentication, CAPTCHA, DRM, or license servers. If an episode appears DRM-protected, it is skipped and recorded as failed.

The Chromium fallback does not solve access challenges or CAPTCHAs. It simply loads the ordinary page and observes media requests that page already receives.

The adapter does not blur, crop, or erase burned-in watermarks. It saves the source media exposed by the player.

## Reliability

DramaFren may block datacenter requests or change its markup/player. The personal local worker is therefore preferred over GitHub-hosted runners. If a normal session is required for content you are authorized to access, `TOKISCLONE_COOKIE_FILE` can point to your own Netscape-format cookies file.
