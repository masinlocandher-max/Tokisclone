# Bulk Download MVP

Tokisclone's bulk MVP follows two fixed product rules:

- **Source policy: `clean-only`**
- **Profile scope: `all_public`**

Neither is configurable by a job.

## Profile flow

```text
one creator/profile URL
        ↓
discover every public video the extractor can enumerate
        ↓
deduplicate
        ↓
skip already archived IDs when possible
        ↓
strict clean-only source selection
        ↓
download
        ↓
retry failures once
        ↓
manifest / per-item results
        ↓
optional Drive archive and transcription
```

A profile job does not need `limit`, `max_items`, or `watermark_policy`.

## Clean-only rule

Tokisclone excludes TikTok's known marked `download` rendition from its format selector and does not add a generic marked fallback. It also checks returned format metadata and rejects a result identified as watermarked.

Tokisclone does not blur, crop, paint over, or digitally erase a burned-in watermark. If a clean source is not available to the current extractor/session, that item should fail rather than be silently saved as a marked video.

## GitHub Actions profile job

```json
{
  "kind": "profile",
  "profile_url": "https://www.tiktok.com/@creator",
  "quality": "best",
  "write_subtitles": false,
  "retry_failed_once": true
}
```

`bulk_profile` is accepted as an alias with identical behavior.

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

## Personal Drive worker profile job

```json
{
  "kind": "profile",
  "platform": "tiktok",
  "profile_url": "https://www.tiktok.com/@creator",
  "retry_failed_once": true,
  "transcribe": false
}
```

The worker checks known video IDs against the Drive library before downloading, so already archived items can be skipped without wasting bandwidth.

It stores:

- videos
- normalized metadata
- latest profile inventory
- latest bulk manifest
- optional transcripts

## Explicit URL batch

```json
{
  "kind": "bulk_urls",
  "platform": "tiktok",
  "urls": [
    "https://www.tiktok.com/@creator/video/...",
    "https://www.tiktok.com/@creator/video/..."
  ],
  "retry_failed_once": true
}
```

All supplied valid URLs are deduplicated and processed. The same clean-only rule is mandatory.

## MCP behavior

`list_profile_videos(profile_url)` returns the complete public profile inventory discovered by the active extractor.

`sync_creator(profile_url)` attempts to save the complete discovered public profile while skipping videos already present in Drive.

There is no profile limit parameter and no watermark-policy parameter on these MCP tools.

## TikTok profile fallback

Queue/Actions profile jobs may include an optional `seed_video_url` from the same creator. If ordinary TikTok profile discovery fails and the seed exposes a channel ID, Tokisclone attempts the `tiktokuser:<channel_id>` route.

## What "all public videos" means

It means every public video that the current yt-dlp extractor and current network/session can successfully enumerate from the supplied profile at run time.

It does not include private, deleted, region-blocked, age-restricted, login-inaccessible, or otherwise unavailable videos.

## Operational reality

TikTok and other platforms can change their web behavior. Keep yt-dlp current and use only public media or media you are authorized to process.
