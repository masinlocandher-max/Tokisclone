from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yt_dlp


def _clean(info: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": info.get("id"),
        "url": info.get("webpage_url") or info.get("original_url"),
        "title": info.get("title") or info.get("description"),
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
        "thumbnail": info.get("thumbnail"),
        "extractor": info.get("extractor"),
    }


def _ydl(extra: dict[str, Any] | None = None) -> yt_dlp.YoutubeDL:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,
        "socket_timeout": 30,
        "retries": 3,
    }
    if extra:
        opts.update(extra)
    return yt_dlp.YoutubeDL(opts)


def diagnostic(job: dict[str, Any], out: Path) -> dict[str, Any]:
    text = str(job.get("message") or "Tokisclone bridge OK")
    path = out / "diagnostic.txt"
    path.write_text(text + "\n", encoding="utf-8")
    return {"kind": "diagnostic", "ok": True, "file": path.name}


def inspect_url(job: dict[str, Any], out: Path) -> dict[str, Any]:
    url = str(job.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("inspect job requires an http(s) url")

    with _ydl({"noplaylist": True, "skip_download": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    if not isinstance(info, dict):
        raise RuntimeError("yt-dlp returned no metadata")

    metadata = _clean(info)
    path = out / "metadata.json"
    path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"kind": "inspect", "ok": True, "metadata": metadata, "file": path.name}


def download_video(job: dict[str, Any], out: Path) -> dict[str, Any]:
    url = str(job.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("video job requires an http(s) url")

    outtmpl = str(out / "%(uploader_id,uploader|unknown)s_%(id)s.%(ext)s")
    with _ydl(
        {
            "noplaylist": True,
            "outtmpl": outtmpl,
            "format": "best[ext=mp4]/best",
            "writesubtitles": bool(job.get("write_subtitles", False)),
            "writeautomaticsub": bool(job.get("write_subtitles", False)),
        }
    ) as ydl:
        info = ydl.extract_info(url, download=True)

    if not isinstance(info, dict):
        raise RuntimeError("yt-dlp returned no video metadata")

    metadata = _clean(info)
    video_id = str(metadata.get("id") or "")
    candidates = [
        p
        for p in out.iterdir()
        if p.is_file()
        and p.name != "result.json"
        and not p.name.endswith((".part", ".ytdl", ".json"))
        and (not video_id or video_id in p.name)
    ]
    candidates.sort(key=lambda p: (-p.stat().st_size, p.name))
    return {"kind": "video", "ok": True, "metadata": metadata, "files": [p.name for p in candidates]}


def _extract_profile(source: str, limit: int) -> dict[str, Any]:
    with _ydl({"extract_flat": True, "playlistend": limit, "skip_download": True}) as ydl:
        info = ydl.extract_info(source, download=False)
    if not isinstance(info, dict):
        raise RuntimeError("yt-dlp returned no profile data")
    return info


def _channel_id_from_video(seed_video_url: str) -> str | None:
    with _ydl({"noplaylist": True, "skip_download": True}) as ydl:
        info = ydl.extract_info(seed_video_url, download=False)
    if not isinstance(info, dict):
        return None
    channel_id = info.get("channel_id")
    return str(channel_id) if channel_id else None


def _normalize_profile_entries(info: dict[str, Any], profile_url: str) -> list[dict[str, Any]]:
    username = profile_url.rstrip("/").split("/")[-1]
    videos: list[dict[str, Any]] = []
    for entry in info.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        item_url = entry.get("webpage_url") or entry.get("url")
        if item_url and str(item_url).isdigit() and "tiktok" in profile_url.lower() and username.startswith("@"):
            item_url = f"https://www.tiktok.com/{username}/video/{item_url}"
        videos.append(
            {
                "id": entry.get("id"),
                "url": item_url,
                "title": entry.get("title") or entry.get("description"),
                "duration": entry.get("duration"),
                "timestamp": entry.get("timestamp"),
                "view_count": entry.get("view_count"),
                "like_count": entry.get("like_count"),
            }
        )
    return videos


def profile_inventory(job: dict[str, Any], out: Path) -> dict[str, Any]:
    profile_url = str(job.get("profile_url") or "").strip()
    if not profile_url.startswith(("http://", "https://")):
        raise ValueError("profile job requires profile_url")

    limit = max(1, min(int(job.get("limit", 50)), 500))
    seed_video_url = str(job.get("seed_video_url") or "").strip()
    discovery_method = "profile_url"
    primary_error: str | None = None

    try:
        info = _extract_profile(profile_url, limit)
        videos = _normalize_profile_entries(info, profile_url)
    except Exception as exc:
        primary_error = f"{type(exc).__name__}: {exc}"
        videos = []

    if not videos and seed_video_url:
        try:
            channel_id = _channel_id_from_video(seed_video_url)
            if channel_id:
                discovery_method = "seed_video_channel_id"
                info = _extract_profile(f"tiktokuser:{channel_id}", limit)
                videos = _normalize_profile_entries(info, profile_url)
        except Exception:
            videos = []

    if not videos:
        raise RuntimeError(
            "Profile discovery returned no public videos. This can happen when the source site blocks "
            "datacenter traffic; use the personal local_worker.py on your normal connection. "
            f"Primary error: {primary_error or 'empty result'}"
        )

    inventory_path = out / "inventory.json"
    inventory_path.write_text(json.dumps(videos, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "kind": "profile",
        "ok": True,
        "profile_url": profile_url,
        "count": len(videos),
        "discovery_method": discovery_method,
        "primary_error": primary_error,
        "file": inventory_path.name,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("job")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    job_path = Path(args.job)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    job = json.loads(job_path.read_text(encoding="utf-8"))
    kind = str(job.get("kind") or "video").lower()

    try:
        if kind == "diagnostic":
            result = diagnostic(job, out)
        elif kind == "inspect":
            result = inspect_url(job, out)
        elif kind == "video":
            result = download_video(job, out)
        elif kind == "profile":
            result = profile_inventory(job, out)
        else:
            raise ValueError(f"Unsupported job kind: {kind}")
        status = 0
    except Exception as exc:
        result = {"kind": kind, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        status = 1

    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return status


if __name__ == "__main__":
    sys.exit(main())
