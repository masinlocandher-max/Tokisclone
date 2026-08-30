# Personal ChatGPT Mode

This is the recommended Tokisclone setup while it is a single-owner personal tool.

## Architecture

```text
You
 ↓
ChatGPT
 ↓
GitHub connector
 ↓
Tokisclone GitHub Action worker
 ↓
GitHub Actions artifact
 ↓
ChatGPT working runtime
 ↓
Google Drive connector
 ↓
Tokisclone/TikTok, Instagram, YouTube
```

No always-on server is required for this mode. No Google Drive OAuth token needs to be stored in GitHub.

## How it works

1. You give ChatGPT a public video or creator URL and ask it to process/save the content.
2. ChatGPT creates a job JSON file under `jobs/` in this repository.
3. `.github/workflows/process-jobs.yml` runs on GitHub Actions.
4. `process_job.py` uses yt-dlp and ffmpeg to produce the requested result.
5. GitHub stores the result briefly as a workflow artifact.
6. ChatGPT retrieves and extracts the artifact.
7. ChatGPT uploads the resulting media file into the connected Google Drive folder.
8. Temporary runtime files and short-lived GitHub artifacts are not the permanent library. Google Drive is the permanent destination.

## Supported personal jobs

### One video

```json
{
  "kind": "video",
  "url": "https://...",
  "write_subtitles": false
}
```

### Creator/profile inventory

```json
{
  "kind": "profile",
  "profile_url": "https://...",
  "limit": 100
}
```

### Diagnostic

```json
{
  "kind": "diagnostic",
  "message": "Tokisclone bridge OK"
}
```

## Google Drive structure

The current personal library root is `Tokisclone`, with platform folders for TikTok, Instagram, YouTube, plus `exports`.

Creator-specific subfolders can be created as the library grows.

## Why this is the default personal mode

It avoids:

- a TokScript subscription
- an always-on MCP hosting bill
- storing a Google Drive refresh token in GitHub
- building a separate dashboard
- maintaining a database before it is needed

GitHub Actions supplies temporary compute only when a job is submitted. ChatGPT remains the conversational interface and Google Drive remains the archive.

## Direct MCP mode

The repository still contains `server.py`, `drive_storage.py`, `authorize_drive.py`, and Docker support for a future always-on MCP deployment. That mode is optional and is not required for the current personal ChatGPT workflow.

## Security and rights

This workflow is for public content that the owner is permitted to download, archive, transcribe, or otherwise process. It does not include private-account access, CAPTCHA bypassing, DRM circumvention, credential theft, or anti-bot evasion.

The repository should be made private before using it for real job URLs or other personal workflow data.
