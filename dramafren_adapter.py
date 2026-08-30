from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import yt_dlp

DRAMAFREN_HOSTS = {
    "dramabox.dramafren.org",
    "www.dramabox.dramafren.org",
}

MEDIA_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".m3u8", ".mpd"}
MEDIA_URL_RE = re.compile(
    r"https?://[^\s\"'<>]+?(?:\.m3u8|\.mpd|\.mp4)(?:\?[^\s\"'<>]*)?",
    re.I,
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
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
    return (
        "https://dramabox.dramafren.org/index.php?"
        + urlencode({"id": ref.drama_id, "lang": ref.lang, "view": "detail"})
    )


def episode_url(ref: DramaRef, episode: int) -> str:
    if episode < 1:
        raise ValueError("episode must be >= 1")
    return (
        "https://dramabox.dramafren.org/index.php?"
        + urlencode(
            {
                "ep": episode,
                "id": ref.drama_id,
                "lang": ref.lang,
                "view": "watch",
            }
        )
    )


def _client(
    cookie_file: str | None = None,
    extra: dict[str, Any] | None = None,
    *,
    referer: str = "https://dramabox.dramafren.org/",
) -> yt_dlp.YoutubeDL:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "http_headers": {
            "User-Agent": USER_AGENT,
            "Referer": referer,
        },
    }
    if cookie_file:
        cookie_path = Path(cookie_file).expanduser()
        if cookie_path.exists():
            opts["cookiefile"] = str(cookie_path)
    if extra:
        opts.update(extra)
    return yt_dlp.YoutubeDL(opts)


def _netscape_cookies(cookie_file: str | None) -> list[dict[str, Any]]:
    if not cookie_file:
        return []
    path = Path(cookie_file).expanduser()
    if not path.exists():
        return []

    cookies: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            continue
        domain, _flag, cookie_path, secure, expiry, name, value = parts
        item: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain.lstrip("."),
            "path": cookie_path or "/",
            "secure": secure.upper() == "TRUE",
        }
        try:
            exp = float(expiry)
            if exp > 0:
                item["expires"] = exp
        except ValueError:
            pass
        cookies.append(item)
    return cookies


def _browser_page(
    url: str,
    *,
    cookie_file: str | None = None,
    capture_media: bool = False,
) -> tuple[str, list[str]]:
    """Load the ordinary public page in Chromium and observe media requests.

    This is not a challenge/CAPTCHA bypass. If the site presents an access challenge
    or blocks the normal browser session, the adapter fails rather than solving it.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "DramaFren browser fallback requires Playwright. Install requirements-worker.txt "
            "and run: python -m playwright install chromium"
        ) from exc

    media_urls: list[str] = []
    seen: set[str] = set()

    def add_media(candidate: str) -> None:
        value = html.unescape(str(candidate or "").strip())
        if not value.startswith(("http://", "https://")):
            return
        lower = value.lower().split("#", 1)[0]
        if (
            ".m3u8" not in lower
            and ".mpd" not in lower
            and ".mp4" not in lower
        ):
            return
        if value not in seen:
            seen.add(value)
            media_urls.append(value)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT)
        cookies = _netscape_cookies(cookie_file)
        if cookies:
            try:
                context.add_cookies(cookies)
            except Exception:
                pass

        page = context.new_page()

        if capture_media:
            def on_response(response: Any) -> None:
                content_type = str(response.headers.get("content-type") or "").lower()
                response_url = str(response.url)
                if (
                    content_type.startswith("video/")
                    or "mpegurl" in content_type
                    or "dash+xml" in content_type
                    or any(ext in response_url.lower() for ext in (".m3u8", ".mpd", ".mp4"))
                ):
                    add_media(response_url)

            page.on("response", on_response)

        response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
        if response is not None and response.status in {401, 403, 429}:
            browser.close()
            raise RuntimeError(f"DramaFren browser request was blocked with HTTP {response.status}.")

        if capture_media:
            try:
                page.locator("video").evaluate_all(
                    "els => els.forEach(v => { v.muted = true; v.play().catch(() => {}); })"
                )
            except Exception:
                pass
            page.wait_for_timeout(5000)

        document = page.content()

        if capture_media:
            try:
                for value in page.locator("video[src], source[src]").evaluate_all(
                    "els => els.map(el => el.src || el.getAttribute('src')).filter(Boolean)"
                ):
                    add_media(str(value))
            except Exception:
                pass

            for match in MEDIA_URL_RE.findall(html.unescape(document)):
                add_media(match)

        browser.close()

    return document, media_urls


def _download_webpage(url: str, cookie_file: str | None = None) -> str:
    direct_error: Exception | None = None
    try:
        with _client(cookie_file=cookie_file) as ydl:
            request = ydl.urlopen(url)
            raw = request.read()
            charset = "utf-8"
            content_type = request.headers.get("Content-Type", "")
            match = re.search(r"charset=([A-Za-z0-9._-]+)", content_type, re.I)
            if match:
                charset = match.group(1)
            return raw.decode(charset, errors="replace")
    except Exception as exc:
        direct_error = exc

    try:
        document, _ = _browser_page(url, cookie_file=cookie_file, capture_media=False)
        return document
    except Exception as browser_exc:
        raise RuntimeError(
            "DramaFren page could not be loaded directly or through the normal browser fallback. "
            f"Direct: {type(direct_error).__name__}: {direct_error}; "
            f"Browser: {type(browser_exc).__name__}: {browser_exc}"
        ) from browser_exc


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
    plain = re.sub(r"<[^>]+>", " ", html.unescape(document))

    for match in re.finditer(
        r"(?:[?&]|&amp;)ep=(\d+)(?:[^\"'<>]*)(?:[?&]|&amp;)id="
        + re.escape(drama_id),
        document,
        re.I,
    ):
        found.add(int(match.group(1)))

    for match in re.finditer(r"\bEp\s*(\d+)\b", plain, re.I):
        found.add(int(match.group(1)))

    total_match = re.search(r"Total\s*:\s*(\d+)\s*Eps?", plain, re.I)
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
            "DramaFren detail page returned no public episode list. The site may be blocking the normal session or its markup may have changed."
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
    if info.get("has_drm") is True or info.get("is_drm") is True:
        return True

    candidates: list[str] = []
    for key in ("url", "manifest_url"):
        value = info.get(key)
        if value:
            candidates.append(str(value))

    for fmt in info.get("formats") or []:
        if not isinstance(fmt, dict):
            continue
        if fmt.get("has_drm") is True or fmt.get("is_drm") is True:
            return True
        for key in ("url", "manifest_url"):
            value = fmt.get(key)
            if value:
                candidates.append(str(value))

    for candidate in candidates:
        lower = candidate.lower()
        if any(marker in lower for marker in ("widevine", "playready", "fairplay")):
            return True
        if re.search(r"/(?:drm|license)(?:/|\?|$)", lower):
            return True

    return False


def _extract_page_info(
    url: str,
    *,
    cookie_file: str | None = None,
) -> dict[str, Any]:
    with _client(
        cookie_file=cookie_file,
        extra={"noplaylist": True, "skip_download": True},
        referer=url,
    ) as ydl:
        info = ydl.extract_info(url, download=False)
    if not isinstance(info, dict):
        raise RuntimeError("No media metadata returned.")
    if not (info.get("formats") or info.get("url")):
        raise RuntimeError("No downloadable media formats were exposed.")
    return info


def _resolve_episode_target(
    page_url: str,
    *,
    cookie_file: str | None = None,
) -> tuple[str, dict[str, Any], str]:
    direct_error: Exception | None = None
    try:
        info = _extract_page_info(page_url, cookie_file=cookie_file)
        if _looks_drm_protected(info):
            raise RuntimeError("DramaFren episode appears DRM-protected.")
        return page_url, info, "yt-dlp-page"
    except Exception as exc:
        direct_error = exc

    try:
        _, media_urls = _browser_page(
            page_url,
            cookie_file=cookie_file,
            capture_media=True,
        )
    except Exception as browser_exc:
        raise RuntimeError(
            "DramaFren episode could not be resolved directly or through browser observation. "
            f"Direct: {type(direct_error).__name__}: {direct_error}; "
            f"Browser: {type(browser_exc).__name__}: {browser_exc}"
        ) from browser_exc

    if not media_urls:
        raise RuntimeError(
            "DramaFren public player loaded but exposed no direct MP4/HLS/DASH media request."
        )

    errors: list[str] = []
    for media_url in media_urls:
        try:
            with _client(
                cookie_file=cookie_file,
                extra={"noplaylist": True, "skip_download": True},
                referer=page_url,
            ) as ydl:
                info = ydl.extract_info(media_url, download=False)
            if not isinstance(info, dict):
                continue
            if _looks_drm_protected(info):
                errors.append(f"{media_url}: DRM-protected")
                continue
            return media_url, info, "browser-media-request"
        except Exception as exc:
            errors.append(f"{media_url}: {type(exc).__name__}: {exc}")

    raise RuntimeError(
        "DramaFren media requests were observed but none resolved as direct non-DRM media. "
        + "; ".join(errors[:5])
    )


def inspect_episode(url: str, *, cookie_file: str | None = None) -> dict[str, Any]:
    value = _require_dramafren_url(url)
    target, info, resolver = _resolve_episode_target(value, cookie_file=cookie_file)

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
        "webpage_url": value,
        "resolved_media_url": target if target != value else None,
        "resolver": resolver,
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
    if episode < 1:
        raise ValueError("DramaFren episode URL must contain ep>=1.")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(output_dir / f"episode-{episode:04d}.%(ext)s")

    target, inspected_info, resolver = _resolve_episode_target(
        value,
        cookie_file=cookie_file,
    )
    if _looks_drm_protected(inspected_info):
        raise RuntimeError("DramaFren episode appears DRM-protected; Tokisclone will not download it.")

    with _client(
        cookie_file=cookie_file,
        referer=value,
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
        info = ydl.extract_info(target, download=True)

    if not isinstance(info, dict):
        raise RuntimeError("DramaFren download returned no metadata.")
    if _looks_drm_protected(info):
        raise RuntimeError("Downloaded DramaFren metadata indicates DRM; output rejected.")

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
        "resolved_media_url": target if target != value else None,
        "resolver": resolver,
        "source_policy": "direct-public-non-drm-only",
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
            failures.append(
                {
                    "episode": episode,
                    "url": item["url"],
                    "attempt": 1,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    if failures and retry_failed_once:
        retry_queue = list(failures)
        failures = []
        for failure in retry_queue:
            episode = int(failure["episode"])
            item_dir = output_dir / "episodes" / f"{episode:04d}"
            try:
                result = download_episode(
                    failure["url"],
                    item_dir,
                    cookie_file=cookie_file,
                )
                result["retried"] = True
                items.append(result)
            except Exception as exc:
                failures.append(
                    {
                        "episode": episode,
                        "url": failure["url"],
                        "attempt": 2,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

    items.sort(key=lambda row: int(row.get("episode") or 0))
    failures.sort(key=lambda row: int(row.get("episode") or 0))

    manifest = {
        "platform": "dramafren",
        "drama_id": inventory["drama_id"],
        "title": inventory.get("title"),
        "lang": inventory["lang"],
        "scope": "all_public_episodes",
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
