from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import yt_dlp

DRAMAFREN_HOSTS = {
    "dramabox.dramafren.org",
    "www.dramabox.dramafren.org",
}

MEDIA_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".m3u8", ".mpd"}
DRM_MARKERS = (
    "widevine",
    "playready",
    "fairplay",
    "license",
    "drm",
)


@dataclass(frozen=True)
class DramaRef:
    drama_id: str
    lang: str
    source_url: str


def _require_dramafren_url(url: str) -> str:
    value = str(url or "").strip()
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in DRAMAFREN_HOSTS:
        raise ValueError("DramaFren adapter accepts dramabox.dramafren.org URLs only.")
    return value


def parse_drama_ref(url: str) -> DramaRef:
    value = _require_dramafren_url(url)
    parsed = urlparse(value)
    query = parse_qs(parsed.query)
    drama_id = str((query.get("id") or [""])[0]).strip()
    lang = str((query.get("lang") or ["en"])[0]).strip() or "en"
    if not drama_id.isdigit():
        raise ValueError(
            "DramaFren URL must contain a numeric drama id, for example ?id=42000005228&lang=en&view=detail"
        )
    return DramaRef(drama_id=drama_id, lang=lang, source_url=value)


def detail_url(ref: DramaRef) -> str:
    return f"https://dramabox.dramafren.org/index.php?{urlencode({'id': ref.drama_id, 'lang': ref.lang, 'view': 'detail'})}"


def episode_url(ref: DramaRef, episode: int) -> str:
    if episode < 1:
        raise ValueError("episode must be >= 1")
    return f"https://dramabox.dramafren.org/index.php?{urlencode({'ep': episode, 'id': ref.drama_id, 'lang': ref.lang, 'view': 'watch'})}"


def _client(cookie_file: str | None = None, extra: dict[str, Any] | None = None) -> yt_dlp.YoutubeDL:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
            ),
            "Referer": "https://dramabox.dramafren.org/",
        },
    }
    if cookie_file:
        cookie_path = Path(cookie_file).expanduser()
        if cookie_path.exists():
            opts["cookiefile"] = str(cookie_path)
    if extra:
        opts.update(extra)
    return yt_dlp.YoutubeDL(opts)


def _download_webpage(url: str, cookie_file: str | None = None) -> str:
    with _client(cookie_file=cookie_file) as ydl:
        request = ydl.urlopen(url)
        raw = request.read()
        charset = "utf-8"
        content_type = request.headers.get("Content-Type", "")
        match = re.search(r"charset=([A-Za-z0-9._-]+)", content_type, re.I)
        if match:
            charset = match.group(1)
        return raw.decode(charset, errors="replace")


def _title_from_html(document: str) -> str | None:
    for pattern in (
        r"<h1[^>]*>(.*?)</h1>",
        r"<title[^>]*>(.*?)</title>",
    ):
        match = re.search(pattern, document, re.I | re.S)
        if match:
            value = re.sub(r"<[^>]+>", " ", match.group(1))
            value = html.unescape(re.sub(r"\s+", " ", value)).strip()
            if value and "Dramabox Player" not in value:
                return value
    return None


def _episode_numbers_from_html(document: str, drama_id: str) -> list[int]:
    found: set[int] = set()

    for match in re.finditer(r"(?:[?&]|&amp;)ep=(\d+)(?:[^\"'<>]*)(?:[?&]|&amp;)id=" + re.escape(drama_id), document, re.I):
        found.add(int(match.group(1)))

    for match in re.finditer(r"\bEp\s*(\d+)\b", re.sub(r"<[^>]+>", " ", document), re.I):
        found.add(int(match.group(1)))

    total_match = re.search(r"Total\s*:\s*(\d+)\s*Eps?", re.sub(r"<[^>]+>", " ", document), re.I)
    if total_match:
        total = int(total_match.group(1))
        if total > 0:
            found.update(range(1, total + 1))

    return sorted(n for n in found if n >= 1)


def discover_drama(url: str, *, cookie_file: str | None = None) -> dict[str, Any]:
    ref = parse_drama_ref(url)
    page = detail_url(ref)
    document = _download_webpage(page, cookie_file=cookie_file)
    episodes = _episode_numbers_from_html(document, ref.drama_id)
    if not episodes:
        raise RuntimeError(
            "DramaFren detail page returned no public episode list. The site may be blocking automated requests or its markup may have changed."
        )

    return {
        "platform": "dramafren",
        "drama_id": ref.drama_id,
        "lang": ref.lang,
        "title": _title_from_html(document),
        "detail_url": page,
        "count": len(episodes),
        "episodes": [
            {"episode": number, "url": episode_url(ref, number)}
            for number in episodes
        ],
    }


def _looks_drm_protected(info: dict[str, Any]) -> bool:
    haystack = json.dumps(info, ensure_ascii=False).lower()
    return any(marker in haystack for marker in DRM_MARKERS)


def inspect_episode(url: str, *, cookie_file: str | None = None) -> dict[str, Any]:
    value = _require_dramafren_url(url)
    with _client(
        cookie_file=cookie_file,
        extra={"noplaylist": True, "skip_download": True},
    ) as ydl:
        info = ydl.extract_info(value, download=False)

    if not isinstance(info, dict):
        raise RuntimeError("DramaFren episode returned no media metadata.")
    if _looks_drm_protected(info):
        raise RuntimeError("DramaFren episode appears DRM-protected; Tokisclone will not download it.")

    formats = []
    for fmt in info.get("formats") or []:
        if not isinstance(fmt, dict):
            continue
        formats.append(
            {
                "format_id": fmt.get("format_id"),
                "ext": fmt.get("ext"),
                "protocol": fmt.get("protocol"),
                "width": fmt.get("width"),
                "height": fmt.get("height"),
                "vcodec": fmt.get("vcodec"),
                "acodec": fmt.get("acodec"),
                "url": fmt.get("url"),
            }
        )

    return {
        "id": info.get("id"),
        "title": info.get("title"),
        "webpage_url": info.get("webpage_url") or value,
        "extractor": info.get("extractor"),
        "formats": formats,
    }


def download_episode(
    url: str,
    output_dir: str | Path,
    *,
    cookie_file: str | None = None,
) -> dict[str, Any]:
    value = _require_dramafren_url(url)
    parsed = urlparse(value)
    query = parse_qs(parsed.query)
    episode = int((query.get("ep") or ["0"])[0] or 0)
    ref = parse_drama_ref(value)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(output_dir / f"episode-{episode:04d}.%(ext)s")

    with _client(
        cookie_file=cookie_file,
        extra={
            "noplaylist": True,
            "outtmpl": outtmpl,
            "format": "bv*+ba/b",
            "merge_output_format": "mp4",
            "continuedl": True,
            "writeinfojson": True,
            "writethumbnail": True,
        },
    ) as ydl:
        info = ydl.extract_info(value, download=False)
        if not isinstance(info, dict):
            raise RuntimeError("DramaFren episode returned no media metadata.")
        if _looks_drm_protected(info):
            raise RuntimeError("DramaFren episode appears DRM-protected; Tokisclone will not download it.")
        info = ydl.extract_info(value, download=True)

    if not isinstance(info, dict):
        raise RuntimeError("DramaFren download returned no metadata.")

    media_files = sorted(
        str(path.resolve())
        for path in output_dir.iterdir()
        if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS - {".m3u8", ".mpd"}
    )
    if not media_files:
        raise RuntimeError("DramaFren download produced no media file.")

    return {
        "platform": "dramafren",
        "drama_id": ref.drama_id,
        "lang": ref.lang,
        "episode": episode,
        "source_url": value,
        "status": "downloaded",
        "media_files": media_files,
        "metadata": {
            "id": info.get("id"),
            "title": info.get("title"),
            "extractor": info.get("extractor"),
            "duration": info.get("duration"),
            "width": info.get("width"),
            "height": info.get("height"),
            "ext": info.get("ext"),
        },
    }


def download_drama(
    url: str,
    output_dir: str | Path,
    *,
    cookie_file: str | None = None,
    retry_failed_once: bool = True,
) -> dict[str, Any]:
    inventory = discover_drama(url, cookie_file=cookie_file)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    (output_dir / "inventory.json").write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    items: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for item in inventory["episodes"]:
        episode = int(item["episode"])
        item_dir = output_dir / "episodes" / f"{episode:04d}"
        try:
            items.append(download_episode(item["url"], item_dir, cookie_file=cookie_file))
        except Exception as exc:
            failures.append({
                "episode": episode,
                "url": item["url"],
                "attempt": 1,
                "error": f"{type(exc).__name__}: {exc}",
            })

    if failures and retry_failed_once:
        retry_queue = list(failures)
        failures = []
        for failure in retry_queue:
            episode = int(failure["episode"])
            item_dir = output_dir / "episodes" / f"{episode:04d}"
            try:
                result = download_episode(failure["url"], item_dir, cookie_file=cookie_file)
                result["retried"] = True
                items.append(result)
            except Exception as exc:
                failures.append({
                    "episode": episode,
                    "url": failure["url"],
                    "attempt": 2,
                    "error": f"{type(exc).__name__}: {exc}",
                })

    items.sort(key=lambda row: int(row.get("episode") or 0))
    failures.sort(key=lambda row: int(row.get("episode") or 0))

    manifest = {
        "platform": "dramafren",
        "drama_id": inventory["drama_id"],
        "title": inventory.get("title"),
        "lang": inventory["lang"],
        "discovered": inventory["count"],
        "downloaded": len(items),
        "failed": len(failures),
        "source_policy": "direct-public-non-drm-only",
        "items": items,
        "failures": failures,
    }

    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "failures.json").write_text(
        json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest
