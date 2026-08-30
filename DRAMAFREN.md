# DramaFren Adapter

Tokisclone can process a public DramaFren title/detail/watch URL as one drama job.

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
resolve each episode with yt-dlp
   ↓
reject apparent DRM-protected media
   ↓
download public direct/HLS/DASH media
   ↓
retry failures once
   ↓
inventory + manifest
   ↓
optional Google Drive archive
```

## Job

```json
{
  "kind": "dramafren",
  "url": "https://dramabox.dramafren.org/index.php?id=42000005228&lang=en&view=detail"
}
```

A watch URL containing `id`, `lang`, and `ep` also works; Tokisclone normalizes it to the title detail page and processes the full public drama.

## Output

```text
inventory.json
episodes/
  0001/
  0002/
  ...
manifest.json
failures.json
```

## Safety boundary

Tokisclone only processes media exposed by the public player or an authorized user session. It does not bypass private access, paywalls, authentication, CAPTCHA, DRM, or license servers. If an episode appears DRM-protected, it is skipped and recorded as failed.

The adapter does not blur, crop, or erase burned-in watermarks. It saves the source stream exposed by the player.

## Reliability

DramaFren may block datacenter requests or change its markup. The personal local worker is therefore preferred over GitHub-hosted runners. If a normal browser session is required, `TOKISCLONE_COOKIE_FILE` can point to your own Netscape-format cookies file.
