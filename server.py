from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Literal

import yt_dlp
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("FMB Video MCP")

DEFAULT_DOWNLOAD_DIR = Path(os.getenv("VIDEO_DOWNLOAD_DIR", "/tmp/fmb-video-mcp"))
DEFAULT_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _clean_info(info: dict[str, Any]) -> dict[str, Any]:
    """Return a stable, compact metadata shape instead of yt-dlp's entire payload."""
    return {
        "id": info.get("id"),
        "url": info.get("webpage_url") or info.get("original_url"),
        "title": info.get("title") or info.get("description"),
        "description": info.get("description"),
        "uploader": info.get("uploader"),
        "uploader_id": info.get("uploader_id"),
        "channel": info.get("channel"),
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


def _ydl(extra: dict[str, Any] | None = None) -> yt_dlp.YoutubeDL:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,
    }
    if extra:
        opts.update(extra)
    return yt_dlp.YoutubeDL(opts)


@mcp.tool()
def inspect_video(url: str) -> dict[str, Any]:
    """
    Inspect a public video URL and return normalized metadata.

    Supports sites handled by yt-dlp, including TikTok when its extractor is working.
    Does not attempt to bypass private accounts, login walls, CAPTCHAs, or DRM.
    """
    with _ydl({"noplaylist": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    if not isinstance(info, dict):
        raise RuntimeError("No video metadata was returned.")
    return _clean_info(info)


@mcp.tool()
def list_profile_videos(profile_url: str, limit: int = 50) -> dict[str, Any]:
    """
    Discover public videos from a creator/profile URL on a best-effort basis.

    This depends on the upstream site's public page structure and yt-dlp extractor support.
    It does not bypass private accounts, CAPTCHAs, or authentication barriers.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    limit = min(limit, 500)

    with _ydl({
        "extract_flat": True,
        "playlistend": limit,
        "skip_download": True,
    }) as ydl:
        info = ydl.extract_info(profile_url, download=False)

    if not isinstance(info, dict):
        raise RuntimeError("No profile data was returned.")

    entries = info.get("entries") or []
    results: list[dict[str, Any]] = []

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        item_url = entry.get("webpage_url") or entry.get("url")
        if item_url and str(item_url).isdigit() and "tiktok" in profile_url.lower():
            username = profile_url.rstrip("/").split("/")[-1]
            if username.startswith("@"):
                item_url = f"https://www.tiktok.com/{username}/video/{item_url}"
        results.append({
            "id": entry.get("id"),
            "url": item_url,
            "title": entry.get("title") or entry.get("description"),
            "duration": entry.get("duration"),
            "timestamp": entry.get("timestamp"),
            "view_count": entry.get("view_count"),
            "like_count": entry.get("like_count"),
            "thumbnail": entry.get("thumbnail"),
        })

    return {
        "profile_url": profile_url,
        "count": len(results),
        "videos": results,
    }


@mcp.tool()
def download_video(
    url: str,
    output_dir: str | None = None,
    filename_template: str = "%(uploader)s_%(id)s.%(ext)s",
) -> dict[str, Any]:
    """
    Download a public video that yt-dlp can access.

    Use only for content you own or are authorized to download/use.
    No DRM, private-account, CAPTCHA, or authentication bypass is attempted.
    """
    target_dir = Path(output_dir) if output_dir else DEFAULT_DOWNLOAD_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(target_dir / filename_template)

    with _ydl({
        "noplaylist": True,
        "outtmpl": outtmpl,
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
    }) as ydl:
        info = ydl.extract_info(url, download=True)
        if not isinstance(info, dict):
            raise RuntimeError("Download completed without metadata.")
        requested = info.get("requested_downloads") or []
        filepath = None
        if requested and isinstance(requested, list):
            filepath = requested[0].get("filepath")
        filepath = filepath or ydl.prepare_filename(info)

    return {
        "metadata": _clean_info(info),
        "filepath": filepath,
    }


def _download_audio(url: str, workdir: Path) -> Path:
    outtmpl = str(workdir / "%(id)s.%(ext)s")
    with _ydl({
        "noplaylist": True,
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "wav",
            "preferredquality": "192",
        }],
    }) as ydl:
        info = ydl.extract_info(url, download=True)
        if not isinstance(info, dict):
            raise RuntimeError("Audio extraction returned no metadata.")
        video_id = info.get("id")
        candidates = list(workdir.glob(f"{video_id}*.wav")) if video_id else list(workdir.glob("*.wav"))
        if not candidates:
            raise RuntimeError("Audio conversion failed. Ensure ffmpeg is installed.")
        return candidates[0]


@mcp.tool()
def transcribe_video(
    url_or_path: str,
    model_size: Literal["tiny", "base", "small", "medium", "large-v3"] = "small",
    language: str | None = None,
) -> dict[str, Any]:
    """
    Transcribe a local media file or a public video URL using faster-whisper.

    The model runs locally on the server. First use downloads the selected Whisper model.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "faster-whisper is not installed. Install optional transcription dependencies."
        ) from exc

    with tempfile.TemporaryDirectory(prefix="fmb-video-mcp-") as td:
        workdir = Path(td)
        source = Path(url_or_path)

        if source.exists():
            media_path = source
        elif url_or_path.startswith(("http://", "https://")):
            media_path = _download_audio(url_or_path, workdir)
        else:
            raise ValueError("url_or_path must be an existing local path or an http(s) URL.")

        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        segments, info = model.transcribe(
            str(media_path),
            language=language,
            vad_filter=True,
        )

        rows = []
        transcript_parts = []
        for segment in segments:
            text = segment.text.strip()
            transcript_parts.append(text)
            rows.append({
                "start": round(segment.start, 3),
                "end": round(segment.end, 3),
                "text": text,
            })

        return {
            "language": getattr(info, "language", language),
            "language_probability": getattr(info, "language_probability", None),
            "duration": getattr(info, "duration", None),
            "text": " ".join(transcript_parts).strip(),
            "segments": rows,
        }


@mcp.tool()
def get_bulk_metadata(urls: list[str]) -> dict[str, Any]:
    """Inspect multiple public video URLs and return successes and per-item errors."""
    if len(urls) > 100:
        raise ValueError("Maximum 100 URLs per call.")

    results = []
    errors = []

    for url in urls:
        try:
            results.append(inspect_video(url))
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
        chunks = []
        for i, row in enumerate(records, start=1):
            chunks.append(f"RECORD {i}\n" + "\n".join(f"{k}: {v}" for k, v in row.items()))
        return "\n\n".join(chunks)

    all_keys: list[str] = []
    seen = set()
    for row in records:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                all_keys.append(key)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=all_keys)
    writer.writeheader()
    for row in records:
        writer.writerow({
            k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v
            for k, v in row.items()
        })
    return buf.getvalue()


@mcp.tool()
def health() -> dict[str, Any]:
    """Return a simple health/status response."""
    return {
        "ok": True,
        "name": "FMB Video MCP",
        "tools": [
            "inspect_video",
            "list_profile_videos",
            "download_video",
            "transcribe_video",
            "get_bulk_metadata",
            "export_records",
        ],
    }


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
