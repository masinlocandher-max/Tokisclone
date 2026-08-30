from __future__ import annotations

import json
import mimetypes
import os
import re
import signal
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yt_dlp

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from drive_storage import DriveStorage

POLL_SECONDS = max(5, int(os.getenv("TOKISCLONE_POLL_SECONDS", "15")))
COOKIE_FILE = os.getenv("TOKISCLONE_COOKIE_FILE")
MAX_PROFILE_VIDEOS = max(1, min(int(os.getenv("TOKISCLONE_MAX_PROFILE_VIDEOS", "500")), 500))
ALLOWED_TIKTOK_HOSTS = {
    "tiktok.com",
    "www.tiktok.com",
    "m.tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
}

_STOP = False


def _stop(*_: Any) -> None:
    global _STOP
    _STOP = True


def _safe(value: str, fallback: str = "unknown") -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", (value or fallback).strip())
    return value.strip("._-")[:100] or fallback


def _short_prop(value: Any, limit: int = 120) -> str:
    return str(value or "")[:limit]


def _require_tiktok_url(url: str) -> str:
    value = url.strip()
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in ALLOWED_TIKTOK_HOSTS:
        raise ValueError("Tokisclone local worker currently accepts TikTok URLs only.")
    return value


def _profile_username(profile_url: str) -> str:
    path = urlparse(profile_url).path.rstrip("/")
    last = path.split("/")[-1] if path else ""
    return _safe(last.lstrip("@"), "creator")


def _ydl(extra: dict[str, Any] | None = None) -> yt_dlp.YoutubeDL:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
    }
    if COOKIE_FILE:
        cookie_path = Path(COOKIE_FILE).expanduser()
        if cookie_path.exists():
            options["cookiefile"] = str(cookie_path)
    if extra:
        options.update(extra)
    return yt_dlp.YoutubeDL(options)


def _clean_metadata(info: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": info.get("id"),
        "url": info.get("webpage_url") or info.get("original_url"),
        "title": info.get("title") or info.get("description"),
        "description": info.get("description"),
        "uploader": info.get("uploader"),
        "uploader_id": info.get("uploader_id"),
        "channel": info.get("channel"),
        "channel_id": info.get("channel_id"),
        "duration": info.get("duration"),
        "timestamp": info.get("timestamp"),
        "upload_date": info.get("upload_date"),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "comment_count": info.get("comment_count"),
        "repost_count": info.get("repost_count"),
        "thumbnail": info.get("thumbnail"),
        "tags": info.get("tags"),
        "extractor": info.get("extractor"),
    }


def _canonical_tiktok_url(profile_url: str, entry: dict[str, Any]) -> str | None:
    value = entry.get("webpage_url") or entry.get("url")
    if not value:
        return None
    value = str(value)
    if value.startswith(("http://", "https://")):
        return _require_tiktok_url(value)
    if value.isdigit():
        username = _profile_username(profile_url)
        return f"https://www.tiktok.com/@{username}/video/{value}"
    return None


def _find_downloaded_media(workdir: Path, video_id: str | None) -> Path:
    candidates = []
    for path in workdir.iterdir():
        if not path.is_file():
            continue
        if path.suffix.lower() in {".part", ".ytdl", ".json", ".vtt", ".srt", ".txt"}:
            continue
        if video_id and video_id not in path.name:
            continue
        candidates.append(path)
    if not candidates:
        raise RuntimeError("Video download produced no media file.")
    candidates.sort(key=lambda p: (-p.stat().st_size, p.name))
    return candidates[0]


def _download_one(url: str, workdir: Path) -> tuple[Path, dict[str, Any]]:
    url = _require_tiktok_url(url)
    outtmpl = str(workdir / "%(id)s.%(ext)s")
    with _ydl(
        {
            "noplaylist": True,
            "outtmpl": outtmpl,
            "format": "best[ext=mp4]/best",
        }
    ) as ydl:
        info = ydl.extract_info(url, download=True)
    if not isinstance(info, dict):
        raise RuntimeError("yt-dlp returned no video metadata.")
    metadata = _clean_metadata(info)
    media_path = _find_downloaded_media(workdir, str(metadata.get("id") or "") or None)
    return media_path, metadata


def _transcribe(media_path: Path) -> dict[str, Any]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "Transcription was requested but faster-whisper is not installed. "
            "Install requirements.txt or pip install faster-whisper."
        ) from exc

    model_name = os.getenv("TOKISCLONE_WHISPER_MODEL", "small")
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, info = model.transcribe(str(media_path), vad_filter=True)
    rows: list[dict[str, Any]] = []
    text_parts: list[str] = []
    for segment in segments:
        text = segment.text.strip()
        if text:
            text_parts.append(text)
        rows.append(
            {
                "start": round(segment.start, 3),
                "end": round(segment.end, 3),
                "text": text,
            }
        )
    return {
        "language": getattr(info, "language", None),
        "language_probability": getattr(info, "language_probability", None),
        "duration": getattr(info, "duration", None),
        "text": " ".join(text_parts).strip(),
        "segments": rows,
    }


def _save_video(
    storage: DriveStorage,
    url: str,
    *,
    creator_hint: str | None = None,
    transcribe: bool = False,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="tokisclone-") as td:
        media_path, metadata = _download_one(url, Path(td))
        video_id = str(metadata.get("id") or "").strip()
        if not video_id:
            raise RuntimeError("Downloaded TikTok did not expose a video ID.")

        existing = storage.find_video("tiktok", video_id)
        if existing:
            return {
                "status": "already_saved",
                "video_id": video_id,
                "source_url": url,
                "drive_file": existing,
            }

        creator = _safe(
            str(metadata.get("uploader_id") or metadata.get("uploader") or creator_hint or "creator").lstrip("@"),
            "creator",
        )
        video_folder = storage.creator_subfolder("tiktok", creator, "videos")
        metadata_folder = storage.creator_subfolder("tiktok", creator, "metadata")

        properties = {
            "kind": "video",
            "platform": "tiktok",
            "video_id": _short_prop(video_id),
            "creator": _short_prop(creator),
            "source_url": _short_prop(metadata.get("url") or url),
        }
        mime_type = mimetypes.guess_type(media_path.name)[0] or "video/mp4"
        drive_video = storage.upload_file(
            media_path,
            parent_id=video_folder["id"],
            name=f"{creator}_{video_id}{media_path.suffix.lower() or '.mp4'}",
            mime_type=mime_type,
            properties=properties,
        )
        drive_metadata = storage.upload_json(
            metadata,
            parent_id=metadata_folder["id"],
            name=f"{video_id}.json",
            properties={**properties, "kind": "metadata"},
        )

        transcript_file = None
        if transcribe:
            transcript = _transcribe(media_path)
            transcript_folder = storage.creator_subfolder("tiktok", creator, "transcripts")
            transcript_file = storage.upload_json(
                transcript,
                parent_id=transcript_folder["id"],
                name=f"{video_id}.transcript.json",
                properties={**properties, "kind": "transcript"},
            )

        return {
            "status": "saved",
            "video_id": video_id,
            "creator": creator,
            "source_url": metadata.get("url") or url,
            "drive_video": drive_video,
            "drive_metadata": drive_metadata,
            "drive_transcript": transcript_file,
        }


def _discover_profile(profile_url: str, limit: int) -> list[dict[str, Any]]:
    profile_url = _require_tiktok_url(profile_url)
    with _ydl(
        {
            "extract_flat": True,
            "playlistend": limit,
            "skip_download": True,
        }
    ) as ydl:
        info = ydl.extract_info(profile_url, download=False)
    if not isinstance(info, dict):
        raise RuntimeError("TikTok profile discovery returned no data.")

    videos: list[dict[str, Any]] = []
    for entry in info.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        item_url = _canonical_tiktok_url(profile_url, entry)
        video_id = str(entry.get("id") or "").strip()
        if not item_url or not video_id:
            continue
        videos.append(
            {
                "id": video_id,
                "url": item_url,
                "title": entry.get("title") or entry.get("description"),
                "duration": entry.get("duration"),
                "timestamp": entry.get("timestamp"),
                "view_count": entry.get("view_count"),
                "like_count": entry.get("like_count"),
            }
        )
    if not videos:
        raise RuntimeError(
            "No public TikTok videos were discovered. The account may be empty/private, "
            "or TikTok may require a refreshed yt-dlp build or your own exported cookies."
        )
    return videos


def _process_video_job(storage: DriveStorage, job: dict[str, Any]) -> dict[str, Any]:
    url = _require_tiktok_url(str(job.get("url") or ""))
    return {
        "kind": "video",
        "ok": True,
        "result": _save_video(
            storage,
            url,
            creator_hint=job.get("creator"),
            transcribe=bool(job.get("transcribe", False)),
        ),
    }


def _process_profile_job(storage: DriveStorage, job: dict[str, Any]) -> dict[str, Any]:
    profile_url = _require_tiktok_url(str(job.get("profile_url") or ""))
    requested_limit = int(job.get("limit", 100))
    limit = max(1, min(requested_limit, MAX_PROFILE_VIDEOS))
    download_new_only = bool(job.get("download_new_only", True))
    transcribe = bool(job.get("transcribe", False))
    creator_hint = _profile_username(profile_url)

    inventory = _discover_profile(profile_url, limit)
    found = len(inventory)
    already_saved = 0
    saved = 0
    failed = 0
    details: list[dict[str, Any]] = []

    for item in inventory:
        video_id = str(item["id"])
        if download_new_only:
            existing = storage.find_video("tiktok", video_id)
            if existing:
                already_saved += 1
                details.append(
                    {
                        "video_id": video_id,
                        "url": item["url"],
                        "status": "already_saved",
                        "drive_file": existing,
                    }
                )
                continue
        try:
            result = _save_video(
                storage,
                str(item["url"]),
                creator_hint=creator_hint,
                transcribe=transcribe,
            )
            if result.get("status") == "already_saved":
                already_saved += 1
            else:
                saved += 1
            details.append(result)
        except Exception as exc:
            failed += 1
            details.append(
                {
                    "video_id": video_id,
                    "url": item["url"],
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    metadata_folder = storage.creator_subfolder("tiktok", creator_hint, "metadata")
    inventory_file = storage.upload_json(
        inventory,
        parent_id=metadata_folder["id"],
        name="latest-profile-inventory.json",
        properties={
            "kind": "profile_inventory",
            "platform": "tiktok",
            "creator": _short_prop(creator_hint),
        },
    )

    return {
        "kind": "profile",
        "ok": True,
        "complete": failed == 0,
        "status": "completed" if failed == 0 else "completed_with_errors",
        "profile_url": profile_url,
        "found": found,
        "already_saved": already_saved,
        "saved": saved,
        "failed": failed,
        "inventory_file": inventory_file,
        "items": details,
    }


def process_job(storage: DriveStorage, job: dict[str, Any]) -> dict[str, Any]:
    kind = str(job.get("kind") or "").strip().lower()
    platform = str(job.get("platform") or "tiktok").strip().lower()
    if platform != "tiktok":
        raise ValueError("The personal worker currently supports TikTok jobs only.")
    if kind == "video":
        return _process_video_job(storage, job)
    if kind == "profile":
        return _process_profile_job(storage, job)
    if kind == "diagnostic":
        return {"kind": "diagnostic", "ok": True, "message": job.get("message", "Tokisclone OK")}
    raise ValueError("Unsupported job kind. Use video, profile, or diagnostic.")


def process_queue_once(storage: DriveStorage) -> int:
    queue = storage.ensure_folder("Queue")
    done = storage.ensure_folder("Done")
    failed_folder = storage.ensure_folder("Failed")
    jobs = storage.list_children(queue["id"], name_suffix=".json", limit=100)
    processed = 0

    for drive_job in jobs:
        processed += 1
        job_id = drive_job["id"]
        job_name = drive_job.get("name") or f"{job_id}.json"
        result_parent = done["id"]
        try:
            raw = storage.download_text(job_id)
            job = json.loads(raw)
            if not isinstance(job, dict):
                raise ValueError("Job JSON must contain one object.")
            result = process_job(storage, job)
            if result.get("ok") is False:
                result_parent = failed_folder["id"]
        except Exception as exc:
            result_parent = failed_folder["id"]
            result = {
                "ok": False,
                "job_file": job_name,
                "error": f"{type(exc).__name__}: {exc}",
            }

        result_name = f"{Path(job_name).stem}.result.json"
        storage.upload_json(
            result,
            parent_id=result_parent,
            name=result_name,
            properties={"kind": "job_result", "job_file_id": _short_prop(job_id)},
        )
        storage.move_file(job_id, from_parent=queue["id"], to_parent=result_parent)
        print(json.dumps({"job": job_name, "result": result}, ensure_ascii=False))

    return processed


def main() -> None:
    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)

    storage = DriveStorage()
    status = storage.status()
    print(f"Tokisclone local worker connected to Drive: {status['root_folder'].get('name')}")
    print(f"Watching Queue every {POLL_SECONDS} seconds. Press Ctrl+C to stop.")

    while not _STOP:
        try:
            processed = process_queue_once(storage)
            if processed:
                print(f"Processed {processed} queued job(s).")
        except Exception as exc:
            print(f"Queue error: {type(exc).__name__}: {exc}")
        for _ in range(POLL_SECONDS):
            if _STOP:
                break
            time.sleep(1)

    print("Tokisclone worker stopped.")


if __name__ == "__main__":
    main()
