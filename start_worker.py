#!/usr/bin/env python3
"""One-command launcher for the personal Tokisclone worker.

What it does:
- loads .env when present;
- reuses the local Google Drive OAuth token;
- auto-discovers the top-level Drive folder named "Tokisclone" when the root
  folder id is not already configured;
- ensures a Playwright Chromium browser is installed;
- starts the unified TikTok + DramaFren Drive queue worker.

This launcher does not bypass CAPTCHA, DRM, encryption, paywalls, logins, or
other access controls. DramaFren browser verification, when presented, remains
a manual user action in the visible browser window.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
FOLDER_MIME = "application/vnd.google-apps.folder"
DEFAULT_ROOT_NAME = os.getenv("TOKISCLONE_DRIVE_ROOT_NAME", "Tokisclone")


def _load_credentials() -> Credentials:
    token_json = os.getenv("GOOGLE_DRIVE_TOKEN_JSON")
    token_path = Path(os.getenv("GOOGLE_DRIVE_TOKEN_FILE", "token.json"))

    if token_json:
        creds = Credentials.from_authorized_user_info(json.loads(token_json), [DRIVE_SCOPE])
    else:
        if not token_path.exists():
            client_secret = Path(os.getenv("GOOGLE_CLIENT_SECRET_FILE", "client_secret.json"))
            if client_secret.exists():
                print("Google Drive is not authorized yet. Opening the one-time OAuth flow...")
                subprocess.run([sys.executable, "authorize_drive.py"], check=True)
            if not token_path.exists():
                raise SystemExit(
                    "Google Drive OAuth is not ready. Put your Google OAuth Desktop "
                    "client JSON at client_secret.json (or set GOOGLE_CLIENT_SECRET_FILE), "
                    "then run this launcher again."
                )
        creds = Credentials.from_authorized_user_file(str(token_path), [DRIVE_SCOPE])

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        if not token_json:
            token_path.write_text(creds.to_json(), encoding="utf-8")

    if not creds.valid:
        raise SystemExit("Google Drive OAuth credentials are not valid.")
    return creds


def _discover_root_folder(creds: Credentials) -> dict[str, Any]:
    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    safe_name = DEFAULT_ROOT_NAME.replace("'", "\\'")
    query = (
        f"name = '{safe_name}' and mimeType = '{FOLDER_MIME}' "
        "and 'root' in parents and trashed = false"
    )
    files = service.files().list(
        q=query,
        spaces="drive",
        orderBy="modifiedTime desc",
        pageSize=10,
        fields="files(id,name,webViewLink,createdTime,modifiedTime,parents)",
    ).execute().get("files", [])

    if not files:
        raise SystemExit(
            f'Could not find a top-level Google Drive folder named "{DEFAULT_ROOT_NAME}". '
            "Create it first or set GOOGLE_DRIVE_ROOT_FOLDER_ID in .env."
        )

    if len(files) > 1:
        print(
            f'Found {len(files)} top-level folders named "{DEFAULT_ROOT_NAME}"; '
            "using the most recently modified one. Set GOOGLE_DRIVE_ROOT_FOLDER_ID "
            "in .env if you want a different folder."
        )
    return files[0]


def _ensure_browser() -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit(
            "Playwright is not installed. Run: pip install -r requirements-worker.txt"
        ) from exc

    try:
        with sync_playwright() as p:
            executable = Path(p.chromium.executable_path)
            if executable.exists():
                return
    except Exception:
        pass

    print("Installing the Playwright Chromium browser...")
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)


def main() -> None:
    creds = _load_credentials()

    if not os.getenv("GOOGLE_DRIVE_ROOT_FOLDER_ID"):
        root = _discover_root_folder(creds)
        os.environ["GOOGLE_DRIVE_ROOT_FOLDER_ID"] = root["id"]
        print(f"Using Drive root: {root['name']} ({root['id']})")

    _ensure_browser()

    # Import after the environment is resolved so DriveStorage sees the root id.
    from local_worker import main as worker_main

    print("Starting Tokisclone unified Drive worker...")
    worker_main()


if __name__ == "__main__":
    main()
