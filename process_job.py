from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any

import yt_dlp


def _safe(value: str, fallback: str = "item") -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", (value or fallback).strip())
    return value[:120] or fallback


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
    }
    if extra:
        opts.update(extra)
    return yt_dlp.YoutubeDL(opts)


def diagnostic(job: dict[str, Any], out: Path) -> dict[str, Any]:
    text = str(job.get("message") or "Tokisclone bridge OK")
    path = out / "diagnostic.txt"
    path.write_text(text + "\n", encoding="utf-8")
    return {"kind": "diagnostic", "ok": True, "file": path.name}


def tikdata_probe(job: dict[str, Any], out: Path) -> dict[str, Any]:
    """Fetch the advertised OpenAPI schema from the fixed public TikTok API host."""
    url = "https://api.tik-data.com/api/openapi.json"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Tokisclone/1.0 (+personal research tool)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read()
        status = int(getattr(response, "status", 200))
        content_type = response.headers.get("content-type")

    spec = json.loads(raw.decode("utf-8"))
    path = out / "tikdata-openapi.json"
    path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")

    paths = sorted((spec.get("paths") or {}).keys())
    return {
        "kind": "tikdata_probe",
        "ok": True,
        "status": status,
        "content_type": content_type,
        "title": (spec.get("info") or {}).get("title"),
        "version": (spec.get("info") or {}).get("version"),
        "paths": paths,
        "file": path.name,
    }


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
    path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "kind": "inspect",
        "ok": True,
        "metadata": metadata,
        "file": path.name,
    }


def download_video(job: dict[str, Any], out: Path) -> dict[str, Any]:
    url = str(job.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("video job requires an http(s) url")

    outtmpl = str(out / "%(uploader_id,uploader|unknown)s_%(id)s.%(ext)s")
    with _ydl(
        {
            "noplaylist": True,
            "outtmpl": outtmpl,
            "format": "bv*+ba/b",
            "merge_output_format": "mp4",
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

    return {
        "kind": "video",
        "ok": True,
        "metadata": metadata,
        "files": [p.name for p in candidates],
    }


def _extract_profile(source: str, limit: int) -> dict[str, Any]:
    with _ydl(
        {
            "extract_flat": True,
            "playlistend": limit,
            "skip_download": True,
        }
    ) as ydl:
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


def _normalize_profile_entries(
    info: dict[str, Any],
    profile_url: str,
) -> list[dict[str, Any]]:
    username = profile_url.rstrip("/").split("/")[-1]
    videos: list[dict[str, Any]] = []
    for entry in info.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        item_url = entry.get("webpage_url") or entry.get("url")
        if (
            item_url
            and str(item_url).isdigit()
            and "tiktok" in profile_url.lower()
            and username.startswith("@")
        ):
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
        channel_id = _channel_id_from_video(seed_video_url)
        if not channel_id:
            raise RuntimeError(
                "Profile discovery failed and the seed video did not expose a TikTok channel_id. "
                f"Primary error: {primary_error or 'empty profile result'}"
            )
        discovery_method = "seed_video_channel_id"
        info = _extract_profile(f"tiktokuser:{channel_id}", limit)
        videos = _normalize_profile_entries(info, profile_url)

    if not videos and primary_error:
        raise RuntimeError(
            "TikTok profile discovery failed. Provide seed_video_url from the same creator so "
            f"Tokisclone can resolve the secondary channel ID. Primary error: {primary_error}"
        )

    inventory_path = out / "inventory.json"
    inventory_path.write_text(
        json.dumps(videos, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
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
        elif kind == "tikdata_probe":
            result = tikdata_probe(job, out)
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
        result = {
            "kind": kind,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
        status = 1

    (out / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False))
    return status


if __name__ == "__main__":
    sys.exit(main())
