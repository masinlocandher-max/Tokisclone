# Tokisclone Local Worker Setup

This is the recommended setup for the personal, single-owner version of Tokisclone.

## What this does

Your computer becomes the TikTok processing worker while ChatGPT stays the interface and Google Drive stays the permanent library.

```text
You
 ↓
ChatGPT
 ↓
Google Drive / Tokisclone / Queue
 ↓
Tokisclone local worker on your computer
 ↓
TikTok over your normal internet connection
 ↓
Google Drive / Tokisclone / TikTok / creator
```

This avoids the TikTok blocking we observed from GitHub-hosted datacenter runners. It also avoids a TokScript subscription and does not require an always-on public MCP server.

The worker only needs to be running when you want queued jobs processed.

## 1. Make the GitHub repository private

The repository currently contains no Google credentials, but it should still be private before you use it as a personal operational project.

In GitHub:

1. Open `masinlocandher-max/Tokisclone`.
2. Open **Settings**.
3. Scroll to **Danger Zone**.
4. Choose **Change repository visibility**.
5. Change it to **Private**.

Do not attach a self-hosted GitHub Actions runner to this repository while it is public. The recommended Drive-queue worker does not need a GitHub Actions runner at all.

## 2. Install the project on your computer

Install Python 3.12 or newer and Git if they are not already installed.

Clone the repository:

```bash
git clone https://github.com/masinlocandher-max/Tokisclone.git
cd Tokisclone
```

Create a virtual environment.

macOS / Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-minimal.txt
```

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-minimal.txt
```

Use `requirements.txt` instead if you also want local Whisper transcription.

## 3. Configure the local `.env`

Copy `.env.example` to `.env`.

Set the permanent Tokisclone Drive root:

```text
GOOGLE_DRIVE_ROOT_FOLDER_ID=1vgQ0vvrj2_zQPzjhnhnoqOmogY0xcRge
GOOGLE_DRIVE_TOKEN_FILE=token.json
TOKISCLONE_POLL_SECONDS=15
TOKISCLONE_MAX_PROFILE_VIDEOS=500
```

`.env`, `token.json`, `client_secret.json`, and cookie files must remain local and must never be committed to GitHub.

## 4. Create one Google OAuth Desktop client

The ChatGPT Google Drive connection cannot be reused by a program running on your computer. The local worker therefore needs its own Google authorization once.

In Google Cloud Console:

1. Create or select a project for Tokisclone.
2. Enable the **Google Drive API**.
3. Configure the OAuth consent screen for your own Google account.
4. Create an OAuth client with application type **Desktop app**.
5. Download its JSON file.
6. Save it in the Tokisclone project folder as `client_secret.json`.

Do not upload that JSON to GitHub or send it through chat.

## 5. Authorize Google Drive once

With the virtual environment active, run:

```bash
python authorize_drive.py
```

Your browser opens Google's authorization page. Sign into the Google account that owns the Tokisclone Drive folder and approve access.

Tokisclone saves the resulting local authorization as `token.json`.

That token is ignored by Git and should stay only on your computer.

## 6. Start the worker

Run:

```bash
python run_local_worker.py
```

A successful start reports that it connected to the `Tokisclone` Drive root and is watching `Queue`.

The worker then checks the Drive queue periodically. It does not need an inbound internet port, public URL, MCP deployment, or database.

## 7. Use ChatGPT as the interface

Once the worker is running, you can ask ChatGPT things such as:

- `Save this TikTok to Tokisclone: <URL>`
- `Get the latest 50 videos from @creator and save only the new ones.`
- `Sync this TikTok profile.`
- `Download this video and transcribe it.`
- `Check whether video <ID> is already in my Tokisclone library.`
- `Show me the last failed Tokisclone jobs.`

ChatGPT writes a small JSON file into the Drive `Queue` folder. The local worker reads it, performs the job, archives the result, then moves the request to `Done` or `Failed`.

### One-video job

```json
{
  "kind": "video",
  "platform": "tiktok",
  "url": "https://www.tiktok.com/@creator/video/1234567890",
  "transcribe": false
}
```

### Profile-sync job

```json
{
  "kind": "profile",
  "platform": "tiktok",
  "profile_url": "https://www.tiktok.com/@creator",
  "limit": 100,
  "download_new_only": true,
  "transcribe": false
}
```

## Google Drive layout

The worker builds creator folders as needed:

```text
Tokisclone/
├── Queue/
├── Done/
├── Failed/
├── TikTok/
│   └── creator/
│       ├── videos/
│       ├── metadata/
│       └── transcripts/
├── Instagram/
├── YouTube/
└── exports/
```

The worker stores Drive metadata properties for the platform, creator, source URL, and video ID so it can detect previously saved videos.

## Optional TikTok session cookies

Start without cookies. On a normal residential connection, public TikTok extraction may work directly.

If TikTok requires your normal logged-in session for public content, Tokisclone can optionally use a cookies file that you explicitly export on your own computer:

```text
TOKISCLONE_COOKIE_FILE=cookies.txt
```

Keep that file local. Never commit it, upload it to Drive, or send it through chat. This option is for your own normal session only; Tokisclone does not bypass CAPTCHAs, private accounts, DRM, or other access controls.

## Updating Tokisclone

Stop the worker, then run:

```bash
git pull
pip install -r requirements-minimal.txt
python run_local_worker.py
```

Keeping `yt-dlp` current matters because TikTok changes its public website frequently.

## If a job fails

Look in `Tokisclone/Failed` in Google Drive. The result JSON records the error. You can ask ChatGPT to inspect the failed job and diagnose it.

Do not repeatedly retry a TikTok CAPTCHA or access-control failure. If TikTok changes its public site, update the extraction layer instead.
