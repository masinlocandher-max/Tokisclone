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

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from dramafren_queue import process_dramafren_queue_job
from drive_storage import DriveStorage
from media_core import (
    MANDATORY_SOURCE_POLICY,
    clean_metadata,
    discover_profile,
    download_one,
    ydl,
)

POLL_SECONDS = max(5, int(os.getenv("TOKISCLONE_POLL_SECONDS", "15")))
COOKIE_FILE = os.getenv("TOKISCLONE_COOKIE_FILE")

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
        raise ValueError("TikTok job requires a TikTok URL.")
    return value


def _profile_username(profile_url: str) -> str:
    path = urlparse(profile_url).path.rstrip("/")
    last = path.split("/")[-1] if path else ""
    return _safe(last.lstrip("@"), "creator")


def _transcribe(media_path: Path) -> dict[str, Any]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "Transcription was requested but faster-whisper is not installed."
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
    quality: str = "best",
    write_subtitles: bool = False,
) -> dict[str, Any]:
    url = _require_tiktok_url(url)

    with ydl(
        {"noplaylist": True, "skip_download": True},
        cookie_file=COOKIE_FILE,
    ) as client:
        info = client.extract_info(url, download=False)

    if not isinstance(info, dict):
        raise RuntimeError("TikTok returned no video metadata.")

    pre_metadata = clean_metadata(info)
    pre_video_id = str(pre_metadata.get("id") or "").strip()
    if pre_video_id:
        existing = storage.find_video("tiktok", pre_video_id)
        if existing:
            return {
                "status": "already_saved",
                "video_id": pre_video_id,
                "source_url": url,
                "drive_file": existing,
                "source_policy": MANDATORY_SOURCE_POLICY,
            }

    with tempfile.TemporaryDirectory(prefix="tokisclone-") as td:
        result = download_one(
            url,
            Path(td),
            quality=quality,
            write_subtitles=write_subtitles,
            cookie_file=COOKIE_FILE,
        )

        metadata = result["metadata"]
        video_id = str(metadata.get("id") or "").strip()
        if not video_id:
            raise RuntimeError("Downloaded TikTok did not expose a video ID.")

        media_files = [Path(p) for p in result.get("media_files") or []]
        if not media_files:
            raise RuntimeError("Video download produced no media file.")
        media_path = media_files[0]

        existing = storage.find_video("tiktok", video_id)
        if existing:
            return {
                "status": "already_saved",
                "video_id": video_id,
                "source_url": url,
                "drive_file": existing,
                "source_policy": MANDATORY_SOURCE_POLICY,
            }

        creator = _safe(
            str(
                metadata.get("uploader_id")
                or metadata.get("uploader")
                or creator_hint
                or "creator"
            ).lstrip("@"),
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
            "source_policy": MANDATORY_SOURCE_POLICY,
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
            {
                **metadata,
                "tokisclone": {
                    "source_policy": MANDATORY_SOURCE_POLICY,
                    "format_selector": result.get("format_selector"),
                },
            },
            parent_id=metadata_folder["id"],
            name=f"{video_id}.json",
            properties={**properties, "kind": "metadata"},
        )

        transcript_file = None
        if transcribe:
            transcript = _transcribe(media_path)
            transcript_folder = storage.creator_subfolder(
                "tiktok", creator, "transcripts"
            )
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
            "source_policy": MANDATORY_SOURCE_POLICY,
            "drive_video": drive_video,
            "drive_metadata": drive_metadata,
            "drive_transcript": transcript_file,
        }


def _save_with_retry(
    storage: DriveStorage,
    url: str,
    *,
    creator_hint: str | None,
    transcribe: bool,
    quality: str,
    write_subtitles: bool,
    retry_failed_once: bool,
) -> dict[str, Any]:
    try:
        return _save_video(
            storage,
            url,
            creator_hint=creator_hint,
            transcribe=transcribe,
            quality=quality,
            write_subtitles=write_subtitles,
        )
    except Exception as first_exc:
        if not retry_failed_once:
            raise
        try:
            result = _save_video(
                storage,
                url,
                creator_hint=creator_hint,
                transcribe=transcribe,
                quality=quality,
                write_subtitles=write_subtitles,
            )
            result["retried"] = True
            return result
        except Exception as second_exc:
            raise RuntimeError(
                f"first attempt: {type(first_exc).__name__}: {first_exc}; "
                f"retry: {type(second_exc).__name__}: {second_exc}"
            ) from second_exc


def _process_video_job(storage: DriveStorage, job: dict[str, Any]) -> dict[str, Any]:
    url = _require_tiktok_url(str(job.get("url") or ""))
    return {
        "kind": "video",
        "ok": True,
        "source_policy": MANDATORY_SOURCE_POLICY,
        "result": _save_with_retry(
            storage,
            url,
            creator_hint=job.get("creator"),
            transcribe=bool(job.get("transcribe", False)),
            quality=str(job.get("quality") or "best"),
            write_subtitles=bool(job.get("write_subtitles", False)),
            retry_failed_once=bool(job.get("retry_failed_once", True)),
        ),
    }


def _profile_inventory(
    profile_url: str,
    seed_video_url: str | None,
) -> dict[str, Any]:
    return discover_profile(
        profile_url,
        seed_video_url=seed_video_url,
        cookie_file=COOKIE_FILE,
    )


def _process_profile_job(storage: DriveStorage, job: dict[str, Any]) -> dict[str, Any]:
    profile_url = _require_tiktok_url(str(job.get("profile_url") or ""))
    transcribe = bool(job.get("transcribe", False))
    creator_hint = _profile_username(profile_url)
    seed_video_url = str(job.get("seed_video_url") or "").strip() or None
    quality = str(job.get("quality") or "best")
    write_subtitles = bool(job.get("write_subtitles", False))
    retry_failed_once = bool(job.get("retry_failed_once", True))

    inventory_result = _profile_inventory(profile_url, seed_video_url)
    inventory = inventory_result["videos"]

    already_saved = 0
    saved = 0
    failed = 0
    retried = 0
    details: list[dict[str, Any]] = []

    for item in inventory:
        video_id = str(item.get("id") or "")
        item_url = str(item.get("url") or "")

        if not item_url:
            failed += 1
            details.append(
                {
                    "video_id": video_id or None,
                    "status": "failed",
                    "error": "No usable video URL returned.",
                }
            )
            continue

        if video_id:
            existing = storage.find_video("tiktok", video_id)
            if existing:
                already_saved += 1
                details.append(
                    {
                        "video_id": video_id,
                        "url": item_url,
                        "status": "already_saved",
                        "drive_file": existing,
                    }
                )
                continue

        try:
            result = _save_with_retry(
                storage,
                item_url,
                creator_hint=creator_hint,
                transcribe=transcribe,
                quality=quality,
                write_subtitles=write_subtitles,
                retry_failed_once=retry_failed_once,
            )
            if result.get("retried"):
                retried += 1
            if result.get("status") == "already_saved":
                already_saved += 1
            else:
                saved += 1
            details.append(result)
        except Exception as exc:
            failed += 1
            details.append(
                {
                    "video_id": video_id or None,
                    "url": item_url,
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    metadata_folder = storage.creator_subfolder("tiktok", creator_hint, "metadata")
    inventory_file = storage.upload_json(
        inventory_result,
        parent_id=metadata_folder["id"],
        name="latest-profile-inventory.json",
        properties={
            "kind": "profile_inventory",
            "platform": "tiktok",
            "creator": _short_prop(creator_hint),
            "scope": "all_public",
        },
    )

    manifest_payload = {
        "profile_url": profile_url,
        "scope": "all_public",
        "source_policy": MANDATORY_SOURCE_POLICY,
        "found": len(inventory),
        "saved": saved,
        "already_saved": already_saved,
        "failed": failed,
        "retried": retried,
        "items": details,
    }
    manifest_file = storage.upload_json(
        manifest_payload,
        parent_id=metadata_folder["id"],
        name="latest-bulk-manifest.json",
        properties={
            "kind": "bulk_manifest",
            "platform": "tiktok",
            "creator": _short_prop(creator_hint),
            "scope": "all_public",
        },
    )

    return {
        "kind": "profile",
        "ok": True,
        "complete": failed == 0,
        "status": "completed" if failed == 0 else "completed_with_errors",
        "profile_url": profile_url,
        "scope": "all_public",
        "source_policy": MANDATORY_SOURCE_POLICY,
        "quality": quality,
        "discovery_method": inventory_result["discovery_method"],
        "found": len(inventory),
        "already_saved": already_saved,
        "saved": saved,
        "failed": failed,
        "retried": retried,
        "inventory_file": inventory_file,
        "manifest_file": manifest_file,
        "items": details,
    }


def _process_bulk_urls_job(
    storage: DriveStorage,
    job: dict[str, Any],
) -> dict[str, Any]:
    raw_urls = job.get("urls")
    if not isinstance(raw_urls, list):
        raise ValueError("bulk_urls requires a urls array")

    quality = str(job.get("quality") or "best")
    transcribe = bool(job.get("transcribe", False))
    write_subtitles = bool(job.get("write_subtitles", False))
    retry_failed_once = bool(job.get("retry_failed_once", True))

    seen: set[str] = set()
    urls: list[str] = []
    for raw in raw_urls:
        value = _require_tiktok_url(str(raw or ""))
        if value in seen:
            continue
        seen.add(value)
        urls.append(value)

    saved = 0
    already_saved = 0
    failed = 0
    retried = 0
    details: list[dict[str, Any]] = []

    for url in urls:
        try:
            result = _save_with_retry(
                storage,
                url,
                creator_hint=None,
                transcribe=transcribe,
                quality=quality,
                write_subtitles=write_subtitles,
                retry_failed_once=retry_failed_once,
            )
            if result.get("retried"):
                retried += 1
            if result.get("status") == "already_saved":
                already_saved += 1
            else:
                saved += 1
            details.append(result)
        except Exception as exc:
            failed += 1
            details.append(
                {
                    "url": url,
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    exports = storage.ensure_folder("exports")
    manifest = storage.upload_json(
        {
            "source_policy": MANDATORY_SOURCE_POLICY,
            "requested": len(urls),
            "saved": saved,
            "already_saved": already_saved,
            "failed": failed,
            "retried": retried,
            "items": details,
        },
        parent_id=exports["id"],
        name=f"bulk-urls-{int(time.time())}.json",
        properties={"kind": "bulk_manifest", "platform": "tiktok"},
    )

    return {
        "kind": "bulk_urls",
        "ok": True,
        "complete": failed == 0,
        "status": "completed" if failed == 0 else "completed_with_errors",
        "requested": len(urls),
        "saved": saved,
        "already_saved": already_saved,
        "failed": failed,
        "retried": retried,
        "source_policy": MANDATORY_SOURCE_POLICY,
        "quality": quality,
        "manifest_file": manifest,
        "items": details,
    }


def process_job(storage: DriveStorage, job: dict[str, Any]) -> dict[str, Any]:
    kind = str(job.get("kind") or "").strip().lower()
    platform = str(job.get("platform") or "tiktok").strip().lower()

    if kind in {"dramafren", "dramafren_drama", "drama"} or platform == "dramafren":
        return process_dramafren_queue_job(storage, job)

    if platform != "tiktok":
        raise ValueError("Personal worker supports TikTok and DramaFren jobs.")

    if kind == "video":
        return _process_video_job(storage, job)
    if kind in {"profile", "bulk_profile"}:
        return _process_profile_job(storage, job)
    if kind == "bulk_urls":
        return _process_bulk_urls_job(storage, job)
    if kind == "diagnostic":
        return {
            "kind": "diagnostic",
            "ok": True,
            "message": job.get("message", "Tokisclone OK"),
            "source_policy": MANDATORY_SOURCE_POLICY,
        }

    raise ValueError(
        "Unsupported job kind. Use video, profile, bulk_profile, bulk_urls, dramafren, or diagnostic."
    )


def process_queue_once(storage: DriveStorage) -> int:
    queue = storage.ensure_folder("Queue")
    done = storage.ensure_folder("Done")
    failed_folder = storage.ensure_folder("Failed")
    queued_jobs = storage.list_children(
        queue["id"],
        name_suffix=".json",
        limit=100,
    )
    processed = 0

    for drive_job in queued_jobs:
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
                "source_policy": MANDATORY_SOURCE_POLICY,
            }

        result_name = f"{Path(job_name).stem}.result.json"
        storage.upload_json(
            result,
            parent_id=result_parent,
            name=result_name,
            properties={
                "kind": "job_result",
                "job_file_id": _short_prop(job_id),
            },
        )
        storage.move_file(
            job_id,
            from_parent=queue["id"],
            to_parent=result_parent,
        )
        print(json.dumps({"job": job_name, "result": result}, ensure_ascii=False))

    return processed


def main() -> None:
    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)

    storage = DriveStorage()
    status = storage.status()
    print(
        "Tokisclone local worker connected to Drive: "
        f"{status['root_folder'].get('name')}"
    )
    print(
        f"Watching Queue every {POLL_SECONDS} seconds. "
        "Press Ctrl+C to stop."
    )
    print(
        "TikTok: clean-only, all public videos. "
        "DramaFren: one drama URL -> all public listed episodes; "
        "direct video or unencrypted HLS only."
    )

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
