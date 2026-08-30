from __future__ import annotations

import mimetypes
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from dramafren_adapter import discover_drama, download_episode
from drive_storage import DriveStorage

COOKIE_FILE = os.getenv("TOKISCLONE_COOKIE_FILE")
SOURCE_POLICY = "direct-public-non-drm-only"


def _safe(value: str | None, fallback: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._ -]+", "_", str(value or fallback).strip())
    text = re.sub(r"\s+", " ", text).strip(" ._-")
    return text[:100] or fallback


def _short(value: Any, limit: int = 120) -> str:
    return str(value or "")[:limit]


def _episode_key(drama_id: str, episode: int) -> str:
    return f"{drama_id}:{episode}"


def archive_drama_to_drive(
    storage: DriveStorage,
    url: str,
    *,
    retry_failed_once: bool = True,
) -> dict[str, Any]:
    inventory = discover_drama(url, cookie_file=COOKIE_FILE)
    drama_id = str(inventory["drama_id"])
    title = _safe(inventory.get("title"), drama_id)

    video_folder = storage.creator_subfolder("dramafren", title, "videos")
    metadata_folder = storage.creator_subfolder("dramafren", title, "metadata")

    saved = 0
    already_saved = 0
    failed = 0
    retried = 0
    details: list[dict[str, Any]] = []

    for item in inventory["episodes"]:
        episode = int(item["episode"])
        episode_url = str(item["url"])
        key = _episode_key(drama_id, episode)
        existing = storage.find_video("dramafren", key)
        if existing:
            already_saved += 1
            details.append({
                "episode": episode,
                "video_id": key,
                "status": "already_saved",
                "source_url": episode_url,
                "drive_file": existing,
            })
            continue

        attempts = 2 if retry_failed_once else 1
        last_error: Exception | None = None
        completed = False

        for attempt in range(1, attempts + 1):
            try:
                with tempfile.TemporaryDirectory(prefix="tokisclone-dramafren-") as td:
                    result = download_episode(
                        episode_url,
                        Path(td),
                        cookie_file=COOKIE_FILE,
                    )
                    media_files = [Path(p) for p in result.get("media_files") or []]
                    if not media_files:
                        raise RuntimeError("DramaFren episode produced no media file.")
                    media_path = media_files[0]

                    properties = {
                        "kind": "video",
                        "platform": "dramafren",
                        "video_id": _short(key),
                        "creator": _short(title),
                        "drama_id": _short(drama_id),
                        "episode": str(episode),
                        "source_url": _short(episode_url),
                        "source_policy": SOURCE_POLICY,
                    }
                    mime_type = mimetypes.guess_type(media_path.name)[0] or "video/mp4"
                    drive_video = storage.upload_file(
                        media_path,
                        parent_id=video_folder["id"],
                        name=f"Episode {episode:04d}{media_path.suffix.lower() or '.mp4'}",
                        mime_type=mime_type,
                        properties=properties,
                    )
                    drive_metadata = storage.upload_json(
                        {
                            **result,
                            "tokisclone": {
                                "source_policy": SOURCE_POLICY,
                                "drama_id": drama_id,
                                "episode": episode,
                            },
                        },
                        parent_id=metadata_folder["id"],
                        name=f"episode-{episode:04d}.json",
                        properties={**properties, "kind": "metadata"},
                    )

                saved += 1
                if attempt > 1:
                    retried += 1
                details.append({
                    "episode": episode,
                    "video_id": key,
                    "status": "saved",
                    "source_url": episode_url,
                    "retried": attempt > 1,
                    "drive_video": drive_video,
                    "drive_metadata": drive_metadata,
                })
                completed = True
                break
            except Exception as exc:
                last_error = exc

        if not completed:
            failed += 1
            details.append({
                "episode": episode,
                "video_id": key,
                "status": "failed",
                "source_url": episode_url,
                "error": f"{type(last_error).__name__}: {last_error}",
            })

    inventory_file = storage.upload_json(
        inventory,
        parent_id=metadata_folder["id"],
        name="latest-drama-inventory.json",
        properties={
            "kind": "drama_inventory",
            "platform": "dramafren",
            "creator": _short(title),
            "drama_id": _short(drama_id),
        },
    )

    manifest = {
        "platform": "dramafren",
        "drama_id": drama_id,
        "title": title,
        "scope": "all_public_episodes",
        "source_policy": SOURCE_POLICY,
        "discovered": inventory["count"],
        "saved": saved,
        "already_saved": already_saved,
        "failed": failed,
        "retried": retried,
        "items": details,
    }
    manifest_file = storage.upload_json(
        manifest,
        parent_id=metadata_folder["id"],
        name="latest-drama-manifest.json",
        properties={
            "kind": "drama_manifest",
            "platform": "dramafren",
            "creator": _short(title),
            "drama_id": _short(drama_id),
        },
    )

    return {
        "kind": "dramafren",
        "ok": True,
        "complete": failed == 0,
        "status": "completed" if failed == 0 else "completed_with_errors",
        "platform": "dramafren",
        "drama_id": drama_id,
        "title": title,
        "scope": "all_public_episodes",
        "source_policy": SOURCE_POLICY,
        "found": inventory["count"],
        "saved": saved,
        "already_saved": already_saved,
        "failed": failed,
        "retried": retried,
        "inventory_file": inventory_file,
        "manifest_file": manifest_file,
        "items": details,
    }
