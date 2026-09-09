# Authorized TikTok source download

Tokisclone now supports an **authorized TikTok download path** using TikTok's official Data Portability API for a TikTok user's own posts.

This path is separate from the existing public-URL `yt-dlp` path.

## What it does

1. A user authorizes the TikTok app with the `portability.postsandprofile.single` or compatible ongoing scope.
2. The caller supplies that user's access token to Tokisclone through the `X-TikTok-Access-Token` request header.
3. Tokisclone creates a video portability export request.
4. After TikTok marks the request ready, Tokisclone downloads the official export ZIP.
5. Tokisclone extracts the `Posted Video Download Link` records from the export.
6. The selected source is downloaded byte-for-byte and returned as an MP4 response.

Tokisclone does **not** upscale, re-encode, crop, blur, cover, or remove a watermark. It also does not add one. Resolution is reported as verified only when `ffprobe` is available and can read the downloaded source.

## Current TikTok availability

As of September 2026, TikTok documents the Data Portability API as available to qualified apps globally but currently covering TikTok users in the **EEA and UK**. The app also needs TikTok Login Kit, Webhooks, Data Portability approval, and the relevant portability scopes before this flow can be used in production.

Official references:

- https://developers.tiktok.com/docs/en/data-portability-api-get-started
- https://developers.tiktok.com/docs/en/data-portability-api-add-data-request
- https://developers.tiktok.com/docs/en/data-portability-api-check-status-of-data-request
- https://developers.tiktok.com/docs/en/data-portability-api-download
- https://developers.tiktok.com/docs/en/data-portability-data-types

## Security model

Two credentials are required for these REST routes:

- `X-API-Key`: the private Tokisclone server API key
- `X-TikTok-Access-Token`: the TikTok user's short-lived authorization token

Do not expose `TOKISCLONE_API_KEY`, TikTok client secrets, access tokens, or refresh tokens in browser JavaScript.

Authorized media URLs are validated before proxying. Tokisclone rejects non-HTTPS targets, embedded URL credentials, and destinations resolving to private, loopback, link-local, reserved, multicast, or unspecified IP ranges. Redirect targets are revalidated.

Responses derived from the user's TikTok export use `Cache-Control: no-store`.

## REST flow

Start the REST service:

```bash
python run_rest_api.py
```

### 1. Create the user's video export request

```bash
curl -X POST http://localhost:8080/v1/tiktok/authorized/request \
  -H "X-API-Key: $TOKISCLONE_API_KEY" \
  -H "X-TikTok-Access-Token: $TIKTOK_USER_ACCESS_TOKEN"
```

Keep the `request_id` returned by TikTok.

### 2. Check export status

```bash
curl -X POST http://localhost:8080/v1/tiktok/authorized/status \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $TOKISCLONE_API_KEY" \
  -H "X-TikTok-Access-Token: $TIKTOK_USER_ACCESS_TOKEN" \
  -d '{"request_id":123456789}'
```

Do not call the download routes until TikTok reports the export ready.

### 3. List authorized posted-video links

```bash
curl -X POST http://localhost:8080/v1/tiktok/authorized/links \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $TOKISCLONE_API_KEY" \
  -H "X-TikTok-Access-Token: $TIKTOK_USER_ACCESS_TOKEN" \
  -d '{"request_id":123456789}'
```

The response assigns a zero-based `index` to each `Posted Video Download Link` discovered in the official JSON export.

### 4. Download one source video

```bash
curl -X POST http://localhost:8080/v1/tiktok/authorized/download \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $TOKISCLONE_API_KEY" \
  -H "X-TikTok-Access-Token: $TIKTOK_USER_ACCESS_TOKEN" \
  -d '{"request_id":123456789,"index":0}' \
  --output video.mp4
```

Tokisclone writes the source only to a temporary file, serves it, then deletes that file after the response completes.

## Quality behavior

The authorized path preserves the exact bytes TikTok supplies through the portability download link. It never silently upscales a lower-resolution source and never labels an unverified source as HD.

When `ffprobe` is available, the download response includes:

- `X-Tokisclone-Resolution-Verified`
- `X-Tokisclone-Width`
- `X-Tokisclone-Height`

The Docker image already installs `ffmpeg`, which includes `ffprobe`.

## Limits

Defaults:

```text
TIKTOK_PORTABILITY_MAX_EXPORT_BYTES=268435456
TIKTOK_AUTHORIZED_MAX_VIDEO_BYTES=2147483648
```

Override these only when the deployment has enough memory, temporary disk, bandwidth, and request duration for larger media.
