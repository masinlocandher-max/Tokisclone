# FMB Video MCP

A clean-room, low-cost MCP server for public short-form video research and media processing.

It is **not** TokScript code and does not connect to TokScript. It recreates a useful subset of the workflow with open tooling.

## MVP tools

- `inspect_video`
- `list_profile_videos`
- `download_video`
- `transcribe_video`
- `get_bulk_metadata`
- `export_records`
- `health`

## What this MVP is for

Typical workflow:

1. Give the MCP a public TikTok profile URL.
2. Call `list_profile_videos`.
3. Extract the returned canonical/public URLs.
4. Inspect selected URLs with `inspect_video` or `get_bulk_metadata`.
5. Download authorized content with `download_video`.
6. Transcribe with `transcribe_video`.
7. Export structured data with `export_records`.

## Important limitation

TikTok's official Display API is designed around authorized TikTok users and their own videos. Arbitrary public-profile discovery therefore cannot be implemented solely with the official API.

This MVP uses `yt-dlp` for best-effort public extraction. Site changes can temporarily break profile/video extraction. It intentionally does not include CAPTCHA bypassing, private-account access, DRM bypassing, credential theft, or anti-bot evasion.

## Run locally

Install Python 3.12+ and ffmpeg.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python server.py
```

The MCP server uses Streamable HTTP.

For a lightweight installation without transcription:

```bash
pip install -r requirements-minimal.txt
```

## Docker

```bash
docker build -t fmb-video-mcp .
docker run --rm -p 8000:8000 -v "$PWD/downloads:/data" fmb-video-mcp
```

## Why this can be much cheaper

The core extractor is open source. The MCP server itself has no per-video licensing fee.

Main operating costs are:

- compute/hosting
- storage/bandwidth for downloaded videos
- transcription compute
- maintenance when source sites change

Local transcription can remove per-minute transcription API fees, but large Whisper models need more CPU/RAM and are slower on cheap hosting.

## Production upgrades recommended

Before exposing this on the public internet:

1. Add authentication/OAuth.
2. Add rate limits.
3. Store jobs and metadata in Postgres/Supabase.
4. Put downloaded media in object storage, not the app filesystem.
5. Add a queue for long downloads/transcriptions.
6. Add URL allowlisting and SSRF protections.
7. Add retention/deletion rules.
8. Add logging and usage quotas.
9. Keep `yt-dlp` updated.
10. Separate download workers from the MCP HTTP process.

## Rights / platform rules

Only download or reuse media you own, have permission to use, or are otherwise legally entitled to process. This project deliberately does not attempt to access private content or bypass technical access controls.
