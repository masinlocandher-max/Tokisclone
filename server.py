from __future__ import annotations

import csv
import io
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from drive_storage import DriveStorage
from media_core import (
    MANDATORY_SOURCE_POLICY,
    discover_profile,
    download_one,
    inspect_video_formats,
)

mcp = MCPServer("Tokisclone")
TEMP_ROOT = Path(os.getenv("VIDEO_DOWNLOAD_DIR", "/tmp/tokisclone"))
TEMP_ROOT.mkdir(parents=True, exist_ok=True)
COOKIE_FILE = os.getenv("TOKISCLONE_COOKIE_FILE")


def _platform(url: str, extractor: str | None = None) -> str:
    haystack = f"{url} {extractor or ''}".lower()
    if "tiktok" in haystack:
        return "tiktok"
    if "instagram" in haystack:
        return "instagram"
    if "youtube" in haystack or "youtu.be" in haystack:
        return "youtube"
    return (extractor or "other").lower()


def _safe_name(value: str | None, fallback: str = "unknown") -> str:
    value = (value or fallback).strip().lstrip("@")
    value = re.sub(r"[^A-Za-z0-9._ -]+", "_", value)
    return value[:100] or fallback


def _inspect_video(url: str) -> dict[str, Any]:
    return inspect_video_formats(url, cookie_file=COOKIE_FILE)["video"]


def _list_profile(profile_url: str) -> dict[str, Any]:
    return discover_profile(profile_url, cookie_file=COOKIE_FILE)


def _download_to(url: str, workdir: Path) -> tuple[Path, dict[str, Any]]:
    result = download_one(url, workdir, cookie_file=COOKIE_FILE)
    media_files = [Path(p) for p in result.get("media_files") or []]
    if not media_files:
        raise RuntimeError("Download produced no media file.")
    return media_files[0], result["metadata"]


def _transcribe_media(
    media_path: Path,
    model_size: Literal["tiny", "base", "small", "medium", "large-v3"] = "small",
    language: str | None = None,
) -> dict[str, Any]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "faster-whisper is not installed. Install the full requirements.txt to use transcription."
        ) from exc

    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        str(media_path),
        language=language,
        vad_filter=True,
    )

    rows: list[dict[str, Any]] = []
    text_parts: list[str] = []
    for segment in segments:
        text = segment.text.strip()
        text_parts.append(text)
        rows.append(
            {
                "start": round(segment.start, 3),
                "end": round(segment.end, 3),
                "text": text,
            }
        )

    return {
        "language": getattr(info, "language", language),
        "language_probability": getattr(info, "language_probability", None),
        "duration": getattr(info, "duration", None),
        "text": " ".join(text_parts).strip(),
        "segments": rows,
    }


def _save_video_to_drive(
    storage: DriveStorage,
    url: str,
    *,
    transcribe: bool = False,
    model_size: Literal["tiny", "base", "small", "medium", "large-v3"] = "small",
    language: str | None = None,
) -> dict[str, Any]:
    metadata = _inspect_video(url)
    video_id = str(metadata.get("id") or "")
    if not video_id:
        raise RuntimeError("Video ID could not be determined.")

    platform = _platform(url, metadata.get("extractor"))
    creator = _safe_name(
        metadata.get("uploader_id")
        or metadata.get("uploader")
        or metadata.get("channel")
    )

    existing = storage.find_video(platform, video_id)
    if existing:
        return {
            "status": "already_saved",
            "metadata": metadata,
            "drive_file": existing,
            "source_policy": MANDATORY_SOURCE_POLICY,
        }

    creator_root = storage.creator_folder(platform, creator)
    videos_folder = storage.ensure_folder("videos", creator_root["id"])
    transcripts_folder = storage.ensure_folder("transcripts", creator_root["id"])

    with tempfile.TemporaryDirectory(
        prefix="tokisclone-",
        dir=str(TEMP_ROOT),
    ) as td:
        media_path, downloaded_metadata = _download_to(url, Path(td))

        properties = {
            "kind": "video",
            "platform": platform,
            "video_id": video_id,
            "creator": creator,
            "source_url": url[:124],
            "source_policy": MANDATORY_SOURCE_POLICY,
        }

        drive_file = storage.upload_file(
            media_path,
            parent_id=videos_folder["id"],
            name=f"{video_id}{media_path.suffix.lower()}",
            mime_type=(
                "video/mp4" if media_path.suffix.lower() == ".mp4" else None
            ),
            properties=properties,
        )

        transcript_file = None
        transcript = None
        if transcribe:
            transcript = _transcribe_media(
                media_path,
                model_size=model_size,
                language=language,
            )
            transcript_file = storage.upload_text(
                transcript["text"],
                parent_id=transcripts_folder["id"],
                name=f"{video_id}.txt",
                properties={**properties, "kind": "transcript"},
            )

    return {
        "status": "saved",
        "metadata": downloaded_metadata,
        "drive_file": drive_file,
        "transcript_file": transcript_file,
        "transcript": transcript,
        "source_policy": MANDATORY_SOURCE_POLICY,
    }


@mcp.tool()
def inspect_video(url: str) -> dict[str, Any]:
    """Inspect one public video URL and return normalized metadata."""
    return _inspect_video(url)


@mcp.tool()
def list_profile_videos(profile_url: str) -> dict[str, Any]:
    """Discover all public videos exposed from a creator/profile URL."""
    return _list_profile(profile_url)


@mcp.tool()
def save_video_to_drive(
    url: str,
    transcribe: bool = False,
    model_size: Literal["tiny", "base", "small", "medium", "large-v3"] = "small",
    language: str | None = None,
) -> dict[str, Any]:
    """Save a public/authorized video using mandatory clean-only source selection."""
    return _save_video_to_drive(
        DriveStorage(),
        url,
        transcribe=transcribe,
        model_size=model_size,
        language=language,
    )


@mcp.tool()
def sync_creator(
    profile_url: str,
    transcribe: bool = False,
) -> dict[str, Any]:
    """Discover all public videos from one profile and save every missing video."""
    profile = _list_profile(profile_url)
    storage = DriveStorage()
    saved: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for item in profile["videos"]:
        url = item.get("url")
        video_id = str(item.get("id") or "")

        if not url:
            errors.append(
                {
                    "video_id": video_id,
                    "error": "No usable video URL returned.",
                }
            )
            continue

        try:
            platform = _platform(profile_url)
            if video_id and storage.find_video(platform, video_id):
                skipped.append(
                    {
                        "video_id": video_id,
                        "url": url,
                        "reason": "already_saved",
                    }
                )
                continue

            result = _save_video_to_drive(
                storage,
                str(url),
                transcribe=transcribe,
            )

            if result["status"] == "already_saved":
                skipped.append(
                    {
                        "video_id": video_id,
                        "url": url,
                        "reason": "already_saved",
                    }
                )
            else:
                saved.append(
                    {
                        "video_id": video_id,
                        "url": url,
                        "drive_file": result["drive_file"],
                    }
                )
        except Exception as exc:
            errors.append(
                {
                    "video_id": video_id,
                    "url": url,
                    "error": str(exc),
                }
            )

    return {
        "profile_url": profile_url,
        "scope": "all_public",
        "source_policy": MANDATORY_SOURCE_POLICY,
        "discovered": profile["count"],
        "saved": len(saved),
        "already_saved": len(skipped),
        "failed": len(errors),
        "saved_items": saved,
        "skipped_items": skipped,
        "errors": errors,
    }


@mcp.tool()
def list_drive_library(
    platform: str | None = None,
    creator: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """List Tokisclone files already stored in Google Drive."""
    files = DriveStorage().list_library(
        platform=platform,
        creator=creator,
        limit=limit,
    )
    return {
        "count": len(files),
        "files": files,
    }


@mcp.tool()
def get_saved_video(
    video_id: str,
    platform: str = "tiktok",
) -> dict[str, Any]:
    """Find a previously saved video by platform and video ID."""
    item = DriveStorage().find_video(platform.lower(), video_id)
    return {
        "found": item is not None,
        "file": item,
    }


@mcp.tool()
def transcribe_video(
    url_or_path: str,
    model_size: Literal["tiny", "base", "small", "medium", "large-v3"] = "small",
    language: str | None = None,
) -> dict[str, Any]:
    """Transcribe a local media path or public video URL with local faster-whisper."""
    source = Path(url_or_path)
    if source.exists():
        return _transcribe_media(
            source,
            model_size=model_size,
            language=language,
        )

    if not url_or_path.startswith(("http://", "https://")):
        raise ValueError("url_or_path must be a local path or http(s) URL.")

    with tempfile.TemporaryDirectory(
        prefix="tokisclone-transcribe-",
        dir=str(TEMP_ROOT),
    ) as td:
        media_path, _ = _download_to(url_or_path, Path(td))
        return _transcribe_media(
            media_path,
            model_size=model_size,
            language=language,
        )


@mcp.tool()
def get_bulk_metadata(urls: list[str]) -> dict[str, Any]:
    """Inspect up to 100 public video URLs and return successes and per-item errors."""
    if len(urls) > 100:
        raise ValueError("Maximum 100 URLs per call.")

    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for url in urls:
        try:
            results.append(_inspect_video(url))
        except Exception as exc:
            errors.append({"url": url, "error": str(exc)})

    return {
        "requested": len(urls),
        "succeeded": len(results),
        "failed": len(errors),
        "results": results,
        "errors": errors,
    }


@mcp.tool()
def export_records(
    records: list[dict[str, Any]],
    format: Literal["json", "csv", "txt"] = "json",
) -> str:
    """Export structured records as JSON, CSV, or plain text."""
    if format == "json":
        return json.dumps(records, ensure_ascii=False, indent=2)

    if format == "txt":
        return "\n\n".join(
            f"RECORD {i}\n"
            + "\n".join(f"{key}: {value}" for key, value in row.items())
            for i, row in enumerate(records, start=1)
        )

    keys: list[str] = []
    seen: set[str] = set()
    for row in records:
        for key in row:
            if key not in seen:
                seen.add(key)
                keys.append(key)

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=keys)
    writer.writeheader()
    for row in records:
        writer.writerow(
            {
                key: (
                    json.dumps(value, ensure_ascii=False)
                    if isinstance(value, (list, dict))
                    else value
                )
                for key, value in row.items()
            }
        )
    return buffer.getvalue()


@mcp.tool()
def drive_status() -> dict[str, Any]:
    """Verify authentication to the configured Tokisclone Google Drive root."""
    return DriveStorage().status()


@mcp.tool()
def health() -> dict[str, Any]:
    """Return Tokisclone server health and enforced product rules."""
    return {
        "ok": True,
        "name": "Tokisclone",
        "storage": "Google Drive",
        "source_policy": MANDATORY_SOURCE_POLICY,
        "profile_scope": "all_public",
        "tools": [
            "inspect_video",
            "list_profile_videos",
            "save_video_to_drive",
            "sync_creator",
            "list_drive_library",
            "get_saved_video",
            "transcribe_video",
            "get_bulk_metadata",
            "export_records",
            "drive_status",
        ],
    }


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        stateless_http=True,
        json_response=True,
    )
