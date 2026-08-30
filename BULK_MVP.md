# Bulk Download MVP

Tokisclone's MVP bulk flow is implemented in both the GitHub Actions worker and the personal Google Drive local worker.

## Goal

```text
creator/profile URL
        ↓
discover public videos
        ↓
deduplicate
        ↓
prefer cleaner source rendition
        ↓
download
        ↓
retry failed items once
        ↓
manifest / per-item results
        ↓
optional Drive archive and transcription
```

## Source policy

Jobs accept:

```json
{
  "watermark_policy": "prefer-clean"
}
```

Allowed values:

- `prefer-clean` — first prefer formats excluding TikTok's known `download` rendition, then use a generic fallback if required for compatibility.
- `clean-only` — do not intentionally select that known marked rendition.
- `allow` — normal best-format selection.

Tokisclone does not blur, crop, paint over, or otherwise erase an already burned-in watermark. A clean result depends on the source formats the platform exposes to the current session/region.

## GitHub Actions bulk profile job

Create a job under `jobs/`:

```json
{
  "kind": "bulk_profile",
  "profile_url": "https://www.tiktok.com/@creator",
  "limit": 200,
  "watermark_policy": "prefer-clean",
  "quality": "best",
  "write_subtitles": false,
  "retry_failed_once": true
}
```

The Actions artifact contains:

```text
inventory.json
downloads/
  archive.txt
  manifest.json
  failures.json
  items/
result.json
```

The legacy `profile` job remains inventory-only for compatibility.

## GitHub Actions bulk URL job

```json
{
  "kind": "bulk_urls",
  "urls": [
    "https://www.tiktok.com/@creator/video/...",
    "https://www.youtube.com/shorts/..."
  ],
  "max_items": 500,
  "watermark_policy": "prefer-clean",
  "retry_failed_once": true
}
```

The Actions worker can process platforms supported by the active yt-dlp extractor. Datacenter IP restrictions can still affect platform reliability.

## Personal Drive worker

The personal `local_worker.py` remains TikTok-focused and writes directly to the owner's Google Drive library.

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

Bulk URLs:

```json
{
  "kind": "bulk_urls",
  "platform": "tiktok",
  "urls": [
    "https://www.tiktok.com/@creator/video/..."
  ],
  "watermark_policy": "prefer-clean",
  "retry_failed_once": true
}
```

The Drive worker checks video IDs before download where possible, so already archived videos are skipped without wasting bandwidth.

## TikTok profile fallback

Both paths accept an optional `seed_video_url` from the same creator. When ordinary TikTok profile discovery fails but the video exposes a channel ID, Tokisclone attempts the `tiktokuser:<channel_id>` extractor route.

## Operational reality

This is an MVP, not a promise that TikTok, Instagram, or YouTube will always expose the same endpoints. Keep yt-dlp current and use only public media or media you are authorized to access.
