#!/usr/bin/env python3
"""Ingest a local media file into the Tokisclone private library.

This command intentionally accepts a local file path rather than a third-party
media URL. Use it for files you own or are authorized to store. It verifies the
media with ffprobe, hashes it, uploads it to Google Drive, and records the result
in the local SQLite catalogue.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from drive_storage import DriveStorage
from library_db import LibraryDB, slugify


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_media(path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is required before a file can be marked verified.")
    proc = subprocess.run(
        [
            ffprobe,
            "-v", "error",
            "-show_entries", "format=duration,format_name,size:stream=index,codec_type,codec_name,width,height",
            "-of", "json",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe rejected the media: {proc.stderr.strip() or 'unknown error'}")
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams") or []
    has_video = any(stream.get("codec_type") == "video" for stream in streams)
    if not has_video:
        raise RuntimeError("The file does not contain a video stream.")
    return {"verified": True, "ffprobe": data}


def safe_folder_name(value: str) -> str:
    value = value.strip().replace("/", "-").replace("\\", "-")
    return value[:120] or "Untitled"


def ingest(args: argparse.Namespace) -> dict[str, Any]:
    path = Path(args.file).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)

    verification = verify_media(path)
    sha256 = sha256_file(path)
    bytes_size = path.stat().st_size
    mime_type = mimetypes.guess_type(path.name)[0] or "video/mp4"
    genres = [item.strip() for item in (args.genres or "").split(",") if item.strip()]

    plan = {
        "file": str(path),
        "bytes": bytes_size,
        "sha256": sha256,
        "verified": True,
        "title": args.title,
        "episode": args.episode,
        "platform": args.platform,
    }
    if args.dry_run:
        return {"ok": True, "dry_run": True, "plan": plan, "verification": verification}

    db = LibraryDB(args.db)
    existing = db.find_file_by_sha256(sha256)
    if existing:
        return {
            "ok": True,
            "duplicate": True,
            "message": "A verified file with the same SHA-256 already exists.",
            "file": existing,
        }

    drama = db.upsert_drama(
        title=args.title,
        description=args.description,
        language=args.language,
        genres=genres,
        episode_count=args.episode_count,
    )
    source = db.add_source(
        drama_id=drama["id"],
        platform=args.platform,
        external_id=args.external_id or f"local:{sha256[:16]}",
        source_url=args.source_url,
        status="owned_or_authorized",
        metadata={"ingest": "local-file"},
    )
    episode = db.upsert_episode(
        drama_id=drama["id"],
        episode_number=args.episode,
        title=args.episode_title,
        duration_seconds=None,
    )
    db.link_episode_source(
        episode_id=episode["id"],
        source_id=source["id"],
        playback_url=None,
        status="owned_or_authorized",
        metadata={"local_sha256": sha256},
    )
    job = db.create_job(
        kind="local-file-ingest",
        drama_id=drama["id"],
        source_id=source["id"],
        episode_id=episode["id"],
        state="uploading",
        metadata={"sha256": sha256, "local_name": path.name},
    )

    try:
        storage = DriveStorage(root_folder_id=args.root_folder_id)
        library = storage.ensure_folder("Library")
        platform_folder = storage.ensure_folder(safe_folder_name(args.platform), library["id"])
        title_folder = storage.ensure_folder(safe_folder_name(args.title), platform_folder["id"])
        manifests = storage.ensure_folder("Manifests")

        drive_name = args.drive_name or f"Episode {int(args.episode):03d}{path.suffix.lower() or '.mp4'}"
        uploaded = storage.upload_file(
            path,
            parent_id=title_folder["id"],
            name=drive_name,
            mime_type=mime_type,
            properties={
                "kind": "video",
                "platform": args.platform.lower()[:100],
                "drama_id": drama["id"],
                "episode_id": episode["id"],
                "episode": str(args.episode),
                "sha256": sha256,
                "verified": "1",
            },
        )

        file_record = db.register_file(
            episode_id=episode["id"],
            storage_provider="google_drive",
            provider_file_id=uploaded["id"],
            provider_path=f"Library/{safe_folder_name(args.platform)}/{safe_folder_name(args.title)}/{drive_name}",
            sha256=sha256,
            bytes_size=int(uploaded.get("size") or bytes_size),
            mime_type=uploaded.get("mimeType") or mime_type,
            verified=True,
            verification=verification,
        )

        manifest_payload = {
            "schema_version": 1,
            "drama": drama,
            "source": source,
            "episode": episode,
            "file": file_record,
            "drive": {
                "id": uploaded.get("id"),
                "name": uploaded.get("name"),
                "webViewLink": uploaded.get("webViewLink"),
            },
        }
        manifest = storage.upload_json(
            manifest_payload,
            parent_id=manifests["id"],
            name=f"{slugify(args.title)}-ep{int(args.episode):03d}-{sha256[:12]}.json",
            properties={
                "kind": "library_manifest",
                "drama_id": drama["id"],
                "episode_id": episode["id"],
                "sha256": sha256,
            },
        )
        db.update_job(job["id"], state="complete")
        return {
            "ok": True,
            "duplicate": False,
            "drama": drama,
            "episode": episode,
            "file": file_record,
            "drive": uploaded,
            "manifest": manifest,
        }
    except Exception as exc:
        db.update_job(job["id"], state="failed", error=f"{type(exc).__name__}: {exc}")
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify and ingest an owned/authorized local video into Drive.")
    parser.add_argument("--file", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--episode", required=True, type=int)
    parser.add_argument("--episode-title")
    parser.add_argument("--episode-count", type=int)
    parser.add_argument("--platform", default="owned")
    parser.add_argument("--external-id")
    parser.add_argument("--source-url")
    parser.add_argument("--description")
    parser.add_argument("--language")
    parser.add_argument("--genres", help="Comma-separated genres")
    parser.add_argument("--drive-name")
    parser.add_argument("--root-folder-id", default=os.getenv("SHORT_DRAMA_DRIVE_ROOT_FOLDER_ID"))
    parser.add_argument("--db", default=os.getenv("TOKISCLONE_LIBRARY_DB", "tokisclone_library.db"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.episode <= 0:
        raise SystemExit("--episode must be positive")
    if not args.dry_run and not args.root_folder_id:
        raise SystemExit("Set SHORT_DRAMA_DRIVE_ROOT_FOLDER_ID or pass --root-folder-id.")

    result = ingest(args)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
