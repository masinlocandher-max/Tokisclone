from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import yt_dlp

TIKDATA_BASE = "https://api.tik-data.com"


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


def _public_api_get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    if not path.startswith("/api/v1/public/"):
        raise ValueError("Only fixed public retrieval API paths are allowed")
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{TIKDATA_BASE}{path}?{query}" if query else f"{TIKDATA_BASE}{path}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Tokisclone/1.0 (+personal research tool)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as response:
        raw = response.read()
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("Public API returned a non-object JSON response")
    return data


def _largest_list(value: Any) -> list[Any]:
    best: list[Any] = []
    if isinstance(value, list):
        best = value
        for item in value:
            candidate = _largest_list(item)
            if len(candidate) > len(best):
                best = candidate
    elif isinstance(value, dict):
        for item in value.values():
            candidate = _largest_list(item)
            if len(candidate) > len(best):
                best = candidate
    return best


def diagnostic(job: dict[str, Any], out: Path) -> dict[str, Any]:
    text = str(job.get("message") or "Tokisclone bridge OK")
    path = out / "diagnostic.txt"
    path.write_text(text + "\n", encoding="utf-8")
    return {"kind": "diagnostic", "ok": True, "file": path.name}


def tikdata_probe(job: dict[str, Any], out: Path) -> dict[str, Any]:
    """Fetch the advertised OpenAPI schema from the fixed public TikTok API host."""
    url = f"{TIKDATA_BASE}/api/openapi.json"
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


def tikdata_profile_probe(job: dict[str, Any], out: Path) -> dict[str, Any]:
    username = str(job.get("username") or "").strip().lstrip("@")
    if not username or not re.fullmatch(r"[A-Za-z0-9._]+", username):
        raise ValueError("username must be a TikTok username")

    check = _public_api_get("/api/v1/public/check", {"username": username})
    posts = _public_api_get(
        "/api/v1/public/posts",
        {"username": username, "count": 30, "cursor": 0},
    )

    (out / "profile-check.json").write_text(
        json.dumps(check, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out / "profile-posts.json").write_text(
        json.dumps(posts, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    candidates = _largest_list(posts)
    return {
        "kind": "tikdata_profile_probe",
        "ok": True,
        "username": username,
        "check_keys": sorted(check.keys()),
        "posts_keys": sorted(posts.keys()),
        "largest_list_count": len(candidates),
        "files": ["profile-check.json", "profile-posts.json"],
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
        try:
            channel_id = _channel_id_from_video(seed_video_url)
            if channel_id:
                discovery_method = "seed_video_channel_id"
                info = _extract_profile(f"tiktokuser:{channel_id}", limit)
                videos = _normalize_profile_entries(info, profile_url)
        except Exception:
            pass

    if not videos and "tiktok.com" in profile_url.lower():
        username = profile_url.rstrip("/").split("/")[-1].lstrip("@")
        try:
            posts = _public_api_get(
                "/api/v1/public/posts",
                {"username": username, "count": min(limit, 30), "cursor": 0},
            )
            raw_items = _largest_list(posts)
            videos = [item for item in raw_items if isinstance(item, dict)]
            if videos:
                discovery_method = "public_api_fallback"
                (out / "public-api-raw.json").write_text(
                    json.dumps(posts, ensure_ascii=False, indent=2), encoding="utf-8"
                )
        except Exception:
            videos = []

    if not videos:
        raise RuntimeError(
            "TikTok profile discovery failed through direct extraction and the public API fallback. "
            f"Primary error: {primary_error or 'empty profile result'}"
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
        elif kind == "tikdata_profile_probe":
            result = tikdata_profile_probe(job, out)
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
