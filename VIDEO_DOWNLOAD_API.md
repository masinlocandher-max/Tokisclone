# Tokisclone Video Download REST API

Tokisclone exposes its existing yt-dlp downloader as a small authenticated REST API while keeping Google Drive as the permanent archive.

## Storage flow

```text
Client / server
    |
    v
POST /v1/download
    |
    v
Tokisclone yt-dlp
    |
    v
Temporary local media file
    |
    v
Google Drive / Tokisclone library
```

The temporary file is removed after the request finishes. The durable copy remains in the configured Google Drive root.

## Configuration

Set these values in `.env` or deployment secrets:

```text
GOOGLE_DRIVE_ROOT_FOLDER_ID=your_tokisclone_folder_id
GOOGLE_DRIVE_TOKEN_FILE=token.json
TOKISCLONE_API_KEY=replace_with_a_long_random_secret
REST_HOST=0.0.0.0
REST_PORT=8080
```

For deployment, `GOOGLE_DRIVE_TOKEN_JSON` can be used instead of a local `token.json` file.

Never expose `TOKISCLONE_API_KEY` in browser JavaScript. Call this API from a trusted backend or private client.

## Start

```bash
python run_rest_api.py
```

## Health

```bash
curl http://localhost:8080/health
```

## Inspect a video

```bash
curl -X POST http://localhost:8080/v1/inspect \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $TOKISCLONE_API_KEY" \
  -d '{"url":"https://www.youtube.com/watch?v=VIDEO_ID"}'
```

## Download and save to Drive

```bash
curl -X POST http://localhost:8080/v1/download \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $TOKISCLONE_API_KEY" \
  -d '{"url":"https://www.youtube.com/watch?v=VIDEO_ID"}'
```

Optional transcription:

```json
{
  "url": "https://www.youtube.com/watch?v=VIDEO_ID",
  "transcribe": true,
  "model_size": "small",
  "language": "en"
}
```

Supported URLs depend on the installed yt-dlp version and the source site's current public access behavior. Tokisclone does not bypass DRM, paywalls, authentication requirements, CAPTCHA, or other access controls.

## Response

A successful new download returns the normalized media metadata and the Google Drive file record. If the same platform/video ID is already present in the Tokisclone Drive library, the API returns `already_saved` instead of uploading a duplicate.
