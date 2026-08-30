from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
FOLDER_MIME = "application/vnd.google-apps.folder"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


class DriveStorage:
    """Small Google Drive storage layer for a single-user Tokisclone instance."""

    def __init__(self, root_folder_id: str | None = None) -> None:
        self.root_folder_id = root_folder_id or os.getenv("GOOGLE_DRIVE_ROOT_FOLDER_ID")
        if not self.root_folder_id:
            raise RuntimeError("GOOGLE_DRIVE_ROOT_FOLDER_ID is not configured.")

        self._token_file = os.getenv("GOOGLE_DRIVE_TOKEN_FILE", "token.json")
        self.creds = self._load_credentials()
        self.service = build("drive", "v3", credentials=self.creds, cache_discovery=False)

    def _load_credentials(self) -> Credentials:
        token_json = os.getenv("GOOGLE_DRIVE_TOKEN_JSON")
        if token_json:
            info = json.loads(token_json)
            creds = Credentials.from_authorized_user_info(info, [DRIVE_SCOPE])
        else:
            token_path = Path(self._token_file)
            if not token_path.exists():
                raise RuntimeError(
                    "Google Drive OAuth token not found. Run authorize_drive.py once or set "
                    "GOOGLE_DRIVE_TOKEN_JSON as a secret."
                )
            creds = Credentials.from_authorized_user_file(str(token_path), [DRIVE_SCOPE])

        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            if not token_json:
                Path(self._token_file).write_text(creds.to_json(), encoding="utf-8")
        if not creds.valid:
            raise RuntimeError("Google Drive OAuth credentials are not valid.")
        return creds

    def ensure_folder(self, name: str, parent_id: str | None = None) -> dict[str, Any]:
        parent_id = parent_id or self.root_folder_id
        q = (
            f"name = '{_escape(name)}' and mimeType = '{FOLDER_MIME}' "
            f"and '{_escape(parent_id)}' in parents and trashed = false"
        )
        existing = self.service.files().list(
            q=q,
            spaces="drive",
            fields="files(id,name,webViewLink,parents)",
            pageSize=1,
        ).execute().get("files", [])
        if existing:
            return existing[0]

        return self.service.files().create(
            body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
            fields="id,name,webViewLink,parents",
        ).execute()

    def creator_folder(self, platform: str, creator: str) -> dict[str, Any]:
        platform_name = {
            "tiktok": "TikTok",
            "instagram": "Instagram",
            "youtube": "YouTube",
        }.get(platform.lower(), platform.title())
        platform_folder = self.ensure_folder(platform_name)
        return self.ensure_folder(creator, platform_folder["id"])

    def find_video(self, platform: str, video_id: str) -> dict[str, Any] | None:
        q = (
            "appProperties has { key='tokisclone' and value='1' } and "
            "appProperties has { key='kind' and value='video' } and "
            f"appProperties has {{ key='platform' and value='{_escape(platform.lower())}' }} and "
            f"appProperties has {{ key='video_id' and value='{_escape(video_id)}' }} and "
            "trashed = false"
        )
        files = self.service.files().list(
            q=q,
            spaces="drive",
            fields="files(id,name,mimeType,size,webViewLink,createdTime,appProperties,parents)",
            pageSize=1,
        ).execute().get("files", [])
        return files[0] if files else None

    def upload_file(
        self,
        local_path: str | Path,
        *,
        parent_id: str,
        name: str | None = None,
        mime_type: str | None = None,
        properties: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        path = Path(local_path)
        body: dict[str, Any] = {
            "name": name or path.name,
            "parents": [parent_id],
            "appProperties": {"tokisclone": "1", **(properties or {})},
        }
        media = MediaFileUpload(str(path), mimetype=mime_type, resumable=True)
        return self.service.files().create(
            body=body,
            media_body=media,
            fields="id,name,mimeType,size,webViewLink,webContentLink,createdTime,appProperties,parents",
        ).execute()

    def upload_text(
        self,
        text: str,
        *,
        parent_id: str,
        name: str,
        properties: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        stream = io.BytesIO(text.encode("utf-8"))
        media = MediaIoBaseUpload(stream, mimetype="text/plain", resumable=False)
        body = {
            "name": name,
            "parents": [parent_id],
            "appProperties": {"tokisclone": "1", **(properties or {})},
        }
        return self.service.files().create(
            body=body,
            media_body=media,
            fields="id,name,mimeType,size,webViewLink,createdTime,appProperties,parents",
        ).execute()

    def list_library(
        self,
        *,
        platform: str | None = None,
        creator: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        parts = ["appProperties has { key='tokisclone' and value='1' }", "trashed = false"]
        if platform:
            parts.append(
                f"appProperties has {{ key='platform' and value='{_escape(platform.lower())}' }}"
            )
        if creator:
            parts.append(
                f"appProperties has {{ key='creator' and value='{_escape(creator)}' }}"
            )

        response = self.service.files().list(
            q=" and ".join(parts),
            spaces="drive",
            orderBy="createdTime desc",
            pageSize=min(limit, 1000),
            fields="files(id,name,mimeType,size,webViewLink,createdTime,modifiedTime,appProperties,parents)",
        ).execute()
        return response.get("files", [])[:limit]

    def get_file(self, file_id: str) -> dict[str, Any]:
        return self.service.files().get(
            fileId=file_id,
            fields="id,name,mimeType,size,webViewLink,createdTime,modifiedTime,appProperties,parents",
        ).execute()

    def status(self) -> dict[str, Any]:
        root = self.get_file(self.root_folder_id)
        return {"connected": True, "root_folder": root, "scope": DRIVE_SCOPE}
