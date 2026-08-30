from __future__ import annotations

import argparse
import json
import re
import sys
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


def profile_inventory(job: dict[str, Any], out: Path) -> dict[str, Any]:
    profile_url = str(job.get("profile_url") or "").strip()
    if not profile_url.startswith(("http://", "https://")):
        raise ValueError("profile job requires profile_url")

    limit = max(1, min(int(job.get("limit", 50)), 500))
    with _ydl(
        {
            "extract_flat": True,
            "playlistend": limit,
            "skip_download": True,
        }
    ) as ydl:
        info = ydl.extract_info(profile_url, download=False)

    if not isinstance(info, dict):
        raise RuntimeError("yt-dlp returned no profile data")

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
