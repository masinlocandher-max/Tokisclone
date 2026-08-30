from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

import yt_dlp

MANDATORY_SOURCE_POLICY = "clean-only"
VALID_QUALITIES = {"best", "1080p", "720p"}
MEDIA_EXTENSIONS = {
    ".mp4", ".mkv", ".webm", ".mov", ".m4v",
    ".mp3", ".m4a", ".opus", ".wav", ".aac",
}


def clean_metadata(info: dict[str, Any]) -> dict[str, Any]:
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
        "format_id": info.get("format_id"),
        "format_note": info.get("format_note"),
        "width": info.get("width"),
        "height": info.get("height"),
        "ext": info.get("ext"),
    }


def validate_quality(value: str | None) -> str:
    quality = (value or "best").strip().lower()
    if quality not in VALID_QUALITIES:
        raise ValueError(
            "quality must be one of: "
            + ", ".join(sorted(VALID_QUALITIES))
        )
    return quality


def video_format_selector(
    quality: str = "best",
    watermark_policy: str | None = None,
) -> str:
    """Return Tokisclone's mandatory strict clean-only selector.

    ``watermark_policy`` remains only so older queued jobs do not crash. It is
    deliberately ignored and cannot relax the clean-only rule.
    """
    _ = watermark_policy
    quality = validate_quality(quality)

    height = ""
    if quality == "1080p":
        height = "[height<=1080]"
    elif quality == "720p":
        height = "[height<=720]"

    return (
        f"bv*{height}[format_id!=download]+ba/"
        f"b{height}[format_id!=download]"
    )


def ydl(
    extra: dict[str, Any] | None = None,
    *,
    cookie_file: str | None = None,
) -> yt_dlp.YoutubeDL:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,
        "socket_timeout": 30,
        "retries": 7,
        "fragment_retries": 7,
        "file_access_retries": 3,
    }

    if cookie_file:
        cookie_path = Path(cookie_file).expanduser()
        if cookie_path.exists():
            options["cookiefile"] = str(cookie_path)

    if extra:
        options.update(extra)

    return yt_dlp.YoutubeDL(options)


def _canonical_profile_entry(
    profile_url: str,
    entry: dict[str, Any],
) -> str | None:
    value = entry.get("webpage_url") or entry.get("url")
    if not value:
        return None

    value = str(value)
    if value.startswith(("http://", "https://")):
        return value

    if value.isdigit() and "tiktok" in profile_url.lower():
        username = profile_url.rstrip("/").split("/")[-1]
        if username.startswith("@"):
            return f"https://www.tiktok.com/{username}/video/{value}"

    return None


def _normalize_profile_entries(
    info: dict[str, Any],
    profile_url: str,
) -> list[dict[str, Any]]:
    videos: list[dict[str, Any]] = []
    seen: set[str] = set()

    for entry in info.get("entries") or []:
        if not isinstance(entry, dict):
            continue

        item_url = _canonical_profile_entry(profile_url, entry)
        if not item_url:
            continue

        item_id = str(entry.get("id") or "")
        key = item_id or item_url
        if key in seen:
            continue
        seen.add(key)

        videos.append(
            {
                "id": item_id or None,
                "url": item_url,
                "title": entry.get("title") or entry.get("description"),
                "duration": entry.get("duration"),
                "timestamp": entry.get("timestamp"),
                "view_count": entry.get("view_count"),
                "like_count": entry.get("like_count"),
                "comment_count": entry.get("comment_count"),
                "thumbnail": entry.get("thumbnail"),
            }
        )

    return videos


def _extract_profile(
    source: str,
    *,
    cookie_file: str | None = None,
) -> dict[str, Any]:
    # No playlistend on purpose. One profile means every public entry the
    # current extractor/session can enumerate.
    with ydl(
        {
            "extract_flat": True,
            "skip_download": True,
        },
        cookie_file=cookie_file,
    ) as client:
        info = client.extract_info(source, download=False)

    if not isinstance(info, dict):
        raise RuntimeError("yt-dlp returned no profile data")

    return info


def _channel_id_from_video(
    seed_video_url: str,
    *,
    cookie_file: str | None = None,
) -> str | None:
    with ydl(
        {"noplaylist": True, "skip_download": True},
        cookie_file=cookie_file,
    ) as client:
        info = client.extract_info(seed_video_url, download=False)

    if not isinstance(info, dict):
        return None

    value = info.get("channel_id")
    return str(value) if value else None


def discover_profile(
    profile_url: str,
    limit: int | None = None,
    *,
    seed_video_url: str | None = None,
    cookie_file: str | None = None,
) -> dict[str, Any]:
    # ``limit`` is legacy compatibility only and is intentionally ignored.
    _ = limit
    primary_error: str | None = None
    discovery_method = "profile_url"

    try:
        info = _extract_profile(
            profile_url,
            cookie_file=cookie_file,
        )
        videos = _normalize_profile_entries(info, profile_url)
    except Exception as exc:
        primary_error = f"{type(exc).__name__}: {exc}"
        info = {}
        videos = []

    if not videos and seed_video_url and "tiktok" in profile_url.lower():
        try:
            channel_id = _channel_id_from_video(
                seed_video_url,
                cookie_file=cookie_file,
            )
            if channel_id:
                discovery_method = "seed_video_channel_id"
                info = _extract_profile(
                    f"tiktokuser:{channel_id}",
                    cookie_file=cookie_file,
                )
                videos = _normalize_profile_entries(info, profile_url)
        except Exception as exc:
            if primary_error:
                primary_error += (
                    f"; seed fallback: {type(exc).__name__}: {exc}"
                )
            else:
                primary_error = f"{type(exc).__name__}: {exc}"

    if not videos:
        raise RuntimeError(
            "Profile discovery returned no usable public video URLs. "
            f"Primary error: {primary_error or 'empty result'}"
        )

    return {
        "profile_url": profile_url,
        "scope": "all_public",
        "title": info.get("title"),
        "uploader": info.get("uploader") or info.get("channel"),
        "count": len(videos),
        "discovery_method": discovery_method,
        "primary_error": primary_error,
        "videos": videos,
    }


def inspect_video_formats(
    url: str,
    *,
    cookie_file: str | None = None,
) -> dict[str, Any]:
    with ydl(
        {"noplaylist": True, "skip_download": True},
        cookie_file=cookie_file,
    ) as client:
        info = client.extract_info(url, download=False)

    if not isinstance(info, dict):
        raise RuntimeError("No video metadata returned")

    formats: list[dict[str, Any]] = []
    for fmt in info.get("formats") or []:
        fmt_id = str(fmt.get("format_id") or "")
        fmt_note = str(fmt.get("format_note") or "")
        formats.append(
            {
                "format_id": fmt_id,
                "format_note": fmt_note,
                "ext": fmt.get("ext"),
                "width": fmt.get("width"),
                "height": fmt.get("height"),
                "fps": fmt.get("fps"),
                "tbr": fmt.get("tbr"),
                "vcodec": fmt.get("vcodec"),
                "acodec": fmt.get("acodec"),
                "filesize": fmt.get("filesize") or fmt.get("filesize_approx"),
                "appears_watermarked": (
                    fmt_id.lower() == "download"
                    or "watermark" in fmt_note.lower()
                ),
            }
        )

    return {
        "video": clean_metadata(info),
        "formats": formats,
        "source_policy": MANDATORY_SOURCE_POLICY,
    }


def _format_is_marked(info: dict[str, Any]) -> bool:
    candidates: list[dict[str, Any]] = [info]
    requested = info.get("requested_formats")
    if isinstance(requested, list):
        candidates.extend(
            item for item in requested if isinstance(item, dict)
        )

    for item in candidates:
        fmt_id = str(item.get("format_id") or "").lower()
        fmt_note = str(item.get("format_note") or "").lower()
        if fmt_id == "download" or "watermark" in fmt_note:
            return True

    return False


def _media_candidates(folder: Path, video_id: str | None) -> list[Path]:
    candidates: list[Path] = []
    for path in folder.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in MEDIA_EXTENSIONS:
            continue
        if video_id and video_id not in path.name:
            continue
        candidates.append(path)

    candidates.sort(key=lambda p: (-p.stat().st_size, p.name))
    return candidates


def _purge_output(folder: Path, video_id: str | None) -> None:
    for path in folder.rglob("*"):
        if not path.is_file():
            continue
        if video_id and video_id not in path.name:
            continue
        try:
            path.unlink()
        except OSError:
            pass


def _remove_archive_entry(
    archive_path: str | Path | None,
    video_id: str | None,
) -> None:
    if not archive_path or not video_id:
        return

    path = Path(archive_path)
    if not path.exists():
        return

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        kept = [
            line
            for line in lines
            if not (
                line.strip() == video_id
                or line.strip().endswith(f" {video_id}")
            )
        ]
        path.write_text(
            "\n".join(kept) + ("\n" if kept else ""),
            encoding="utf-8",
        )
    except OSError:
        pass


def download_one(
    url: str,
    output_dir: str | Path,
    *,
    quality: str = "best",
    watermark_policy: str | None = None,
    write_subtitles: bool = False,
    cookie_file: str | None = None,
    archive_path: str | Path | None = None,
) -> dict[str, Any]:
    # ``watermark_policy`` is accepted only so old queued jobs remain readable.
    # It cannot change the mandatory clean-only selector.
    _ = watermark_policy
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    format_selector = video_format_selector(quality)
    outtmpl = str(
        output_dir / "%(uploader_id,uploader|unknown)s_%(id)s.%(ext)s"
    )

    options: dict[str, Any] = {
        "noplaylist": True,
        "outtmpl": outtmpl,
        "format": format_selector,
        "merge_output_format": "mp4",
        "writeinfojson": True,
        "writethumbnail": True,
        "writesubtitles": bool(write_subtitles),
        "writeautomaticsub": bool(write_subtitles),
        "continuedl": True,
    }
    if archive_path:
        options["download_archive"] = str(archive_path)

    with ydl(options, cookie_file=cookie_file) as client:
        info = client.extract_info(url, download=True)

    if not isinstance(info, dict):
        raise RuntimeError("yt-dlp returned no video metadata")

    metadata = clean_metadata(info)
    video_id = str(metadata.get("id") or "") or None

    if _format_is_marked(info):
        _purge_output(output_dir, video_id)
        _remove_archive_entry(archive_path, video_id)
        raise RuntimeError(
            "Source was identified as watermarked; "
            "Tokisclone clean-only policy rejected it."
        )

    media = _media_candidates(output_dir, video_id)
    status = "downloaded" if media else "already_archived_or_no_media"

    return {
        "status": status,
        "url": url,
        "video_id": video_id,
        "source_policy": MANDATORY_SOURCE_POLICY,
        "format_selector": format_selector,
        "metadata": metadata,
        "media_files": [str(p.resolve()) for p in media],
    }


def bulk_download(
    urls: Iterable[str],
    output_dir: str | Path,
    *,
    quality: str = "best",
    watermark_policy: str | None = None,
    write_subtitles: bool = False,
    cookie_file: str | None = None,
    retry_failed_once: bool = True,
    max_items: int | None = None,
) -> dict[str, Any]:
    # Legacy controls are ignored: clean-only is mandatory and the supplied
    # URL list is processed in full.
    _ = watermark_policy
    _ = max_items

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    seen: set[str] = set()
    clean_urls: list[str] = []
    for raw in urls:
        value = str(raw or "").strip()
        if not value or not value.startswith(("http://", "https://")):
            continue
        if value in seen:
            continue
        seen.add(value)
        clean_urls.append(value)

    if not clean_urls:
        raise ValueError("No valid http(s) video URLs were supplied")

    archive_path = output_dir / "archive.txt"
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for index, url in enumerate(clean_urls, start=1):
        item_dir = output_dir / "items" / f"{index:06d}"
        try:
            results.append(
                download_one(
                    url,
                    item_dir,
                    quality=quality,
                    write_subtitles=write_subtitles,
                    cookie_file=cookie_file,
                    archive_path=archive_path,
                )
            )
        except Exception as exc:
            failures.append(
                {
                    "url": url,
                    "attempt": 1,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    if failures and retry_failed_once:
        retry_queue = list(failures)
        failures = []

        for retry_index, item in enumerate(retry_queue, start=1):
            url = item["url"]
            item_dir = output_dir / "retries" / f"{retry_index:06d}"
            try:
                result = download_one(
                    url,
                    item_dir,
                    quality=quality,
                    write_subtitles=write_subtitles,
                    cookie_file=cookie_file,
                    archive_path=archive_path,
                )
                result["retried"] = True
                results.append(result)
            except Exception as exc:
                failures.append(
                    {
                        "url": url,
                        "attempt": 2,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

    summary = {
        "requested": len(clean_urls),
        "succeeded": len(results),
        "failed": len(failures),
        "source_policy": MANDATORY_SOURCE_POLICY,
        "quality": validate_quality(quality),
        "archive": str(archive_path.resolve()),
        "items": results,
        "failures": failures,
    }

    (output_dir / "manifest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "failures.json").write_text(
        json.dumps(failures, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return summary
