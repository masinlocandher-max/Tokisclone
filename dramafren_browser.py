#!/usr/bin/env python3
"""Resolve and verify publicly exposed, non-DRM episode media.

The worker does not bypass authentication, CAPTCHA, DRM, encrypted HLS,
paywalls, or anti-bot verification. It only captures media exposed to a normal
public browser session and can download direct MP4 or unencrypted HLS. For
supported public sites, yt-dlp is used as a normal extractor fallback.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright, Page, Response

MEDIA_EXTENSIONS = (".mp4", ".m3u8", ".mpd")
MEDIA_CONTENT_TYPES = (
    "video/",
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "application/dash+xml",
)


def slugify(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-")
    return value[:100] or "media"


def redact_url(value: str) -> str:
    """Keep host/path and query key names, but never persist query values."""
    try:
        parsed = urllib.parse.urlparse(value)
        keys = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query = urllib.parse.urlencode([(k, "REDACTED") for k, _ in keys])
        return urllib.parse.urlunparse(
            (parsed.scheme, parsed.netloc, parsed.path, parsed.params, query, "")
        )
    except Exception:
        return "[unavailable]"


def is_media_url(url: str) -> bool:
    path = urllib.parse.urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in MEDIA_EXTENSIONS)


def candidate_priority(url: str, content_type: str) -> int:
    u = url.lower()
    ct = (content_type or "").lower()
    if ".mp4" in u or ct.startswith("video/mp4"):
        return 100
    if ".m3u8" in u or "mpegurl" in ct:
        return 80
    if ".mpd" in u or "dash+xml" in ct:
        return 10
    if ct.startswith("video/"):
        return 60
    return 0


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe_media(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "file": path.name,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "verified": False,
    }
    if not shutil.which("ffprobe"):
        result["verification_error"] = "ffprobe unavailable"
        return result
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration,size,format_name:stream=codec_type,codec_name,width,height",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    if proc.returncode != 0:
        result["verification_error"] = proc.stderr[-1000:]
        return result
    try:
        info = json.loads(proc.stdout)
    except json.JSONDecodeError:
        result["verification_error"] = "ffprobe returned invalid JSON"
        return result
    streams = info.get("streams") or []
    has_video = any(stream.get("codec_type") == "video" for stream in streams)
    result["media"] = info
    result["verified"] = bool(has_video and path.stat().st_size > 1024)
    return result


def download_direct(url: str, out: Path, headers: dict[str, str]) -> None:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=120) as response, out.open("wb") as fh:
        shutil.copyfileobj(response, fh)


def fetch_text(url: str, headers: dict[str, str]) -> str:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as response:
        return response.read().decode("utf-8", errors="replace")


def download_hls(url: str, out: Path, headers: dict[str, str]) -> None:
    manifest = fetch_text(url, headers)
    stripped = manifest.lstrip("\ufeff\r\n\t ")
    if not stripped.startswith("#EXTM3U"):
        raise RuntimeError("Candidate URL is not an HLS playlist (#EXTM3U missing)")
    if "#EXT-X-KEY" in manifest.upper():
        raise RuntimeError("Encrypted HLS detected (#EXT-X-KEY); refusing to bypass encryption")
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required for HLS but was not found")
    header_blob = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
            "-headers", header_blob, "-i", url, "-c", "copy", str(out),
        ],
        check=True,
        timeout=1200,
    )


def ytdlp_download(page_url: str, out: Path) -> dict[str, Any]:
    """Normal public-site extractor fallback. No cookies or browser state passed."""
    if not shutil.which("yt-dlp"):
        return {"ok": False, "error": "yt-dlp unavailable"}
    template = str(out.with_suffix(".%(ext)s"))
    proc = subprocess.run(
        [
            "yt-dlp", "--no-playlist", "--no-warnings",
            "--merge-output-format", "mp4",
            "-f", "bestvideo+bestaudio/best",
            "-o", template,
            page_url,
        ],
        capture_output=True,
        text=True,
        timeout=1200,
    )
    matching = sorted(out.parent.glob(out.stem + ".*"), key=lambda p: p.stat().st_size, reverse=True)
    media = next((p for p in matching if p.suffix.lower() in {".mp4", ".webm", ".mkv", ".mov"}), None)
    return {
        "ok": proc.returncode == 0 and media is not None,
        "file": str(media) if media else None,
        "error": None if proc.returncode == 0 else proc.stderr[-1500:],
    }


async def click_first_visible(page: Page, labels: list[str]) -> bool:
    for label in labels:
        selectors = [
            page.get_by_role("button", name=re.compile(re.escape(label), re.I)),
            page.get_by_role("link", name=re.compile(re.escape(label), re.I)),
            page.get_by_text(re.compile(rf"^\s*{re.escape(label)}\s*$", re.I)),
        ]
        for loc in selectors:
            try:
                if await loc.count() and await loc.first.is_visible(timeout=1000):
                    await loc.first.click(timeout=5000)
                    return True
            except Exception:
                pass
    return False


async def resolve_and_download(job: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    detail_url = str(job["detail_url"])
    requested_episode = int(job.get("episode", 1))
    timeout_ms = int(job.get("timeout_ms", 45000))
    output_dir.mkdir(parents=True, exist_ok=True)

    candidates: dict[str, dict[str, str]] = {}
    browser_log: list[dict[str, Any]] = []
    challenge = False
    user_agent = "Mozilla/5.0"
    referer = detail_url
    cookie_header = ""

    async with async_playwright() as p:
        launch_kwargs: dict[str, Any] = {
            "headless": True,
            "args": [
                "--no-sandbox", "--disable-dev-shm-usage",
                "--autoplay-policy=no-user-gesture-required",
            ],
        }
        chrome_bin = os.environ.get("CHROME_BIN")
        if chrome_bin:
            launch_kwargs["executable_path"] = chrome_bin

        browser = await p.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/136.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1365, "height": 900},
            locale="en-US",
        )
        page = await context.new_page()

        async def on_response(response: Response) -> None:
            try:
                ct = response.headers.get("content-type", "")
                url = response.url
                if is_media_url(url) or any(x in ct.lower() for x in MEDIA_CONTENT_TYPES):
                    candidates[url] = {
                        "url": url,
                        "content_type": ct,
                        "status": str(response.status),
                        "source": "network-response",
                    }
            except Exception:
                pass

        page.on("response", on_response)

        try:
            response = await page.goto(detail_url, wait_until="domcontentloaded", timeout=timeout_ms)
            title_now = await page.title()
            body_prefix = ""
            try:
                body_prefix = (await page.locator("body").inner_text(timeout=2500))[:1200]
            except Exception:
                pass
            challenge = (
                (response is not None and response.status in {401, 403, 429})
                and ("just a moment" in title_now.lower() or "verify you are human" in body_prefix.lower())
            )
            browser_log.append({
                "event": "goto",
                "url": redact_url(page.url),
                "status": response.status if response else None,
                "title": title_now,
                "human_verification_gate": challenge,
            })

            if not challenge:
                await page.wait_for_timeout(3000)
                ep_patterns = [f"Ep {requested_episode}", f"EP {requested_episode}", f"Episode {requested_episode}"]
                clicked = await click_first_visible(page, ep_patterns)
                if not clicked:
                    clicked = await click_first_visible(page, ["Start Watching", "Watch Now", "Play Now"])
                browser_log.append({"event": "initial-click", "clicked": clicked, "url": redact_url(page.url)})
                await page.wait_for_timeout(5000)
                await click_first_visible(page, ["Play", "Start", "Resume"])
                try:
                    await page.locator("video").first.click(timeout=2500)
                except Exception:
                    pass
                await page.wait_for_timeout(10000)

                try:
                    dom_sources = await page.eval_on_selector_all(
                        "video, video source, source",
                        "els => els.map(e => e.currentSrc || e.src || e.getAttribute('src')).filter(Boolean)",
                    )
                    for url in dom_sources:
                        absolute = urllib.parse.urljoin(page.url, url)
                        candidates.setdefault(absolute, {
                            "url": absolute, "content_type": "", "status": "", "source": "dom",
                        })
                except Exception:
                    pass

                try:
                    perf_urls = await page.evaluate("performance.getEntriesByType('resource').map(e => e.name)")
                    for url in perf_urls:
                        if is_media_url(url):
                            candidates.setdefault(url, {
                                "url": url, "content_type": "", "status": "", "source": "performance",
                            })
                except Exception:
                    pass

            browser_log.append({
                "event": "final", "url": redact_url(page.url), "title": await page.title(),
                "candidate_count": len(candidates),
            })
            await page.screenshot(path=str(output_dir / "page.png"), full_page=True)

            cookies = await context.cookies()
            cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            user_agent = await page.evaluate("navigator.userAgent")
            referer = page.url
        finally:
            await browser.close()

    ordered = sorted(
        candidates.values(),
        key=lambda item: candidate_priority(item["url"], item.get("content_type", "")),
        reverse=True,
    )

    metadata: dict[str, Any] = {
        "job": {
            "title": job.get("title"),
            "detail_url": redact_url(detail_url),
            "episode": requested_episode,
        },
        "status": "HUMAN_VERIFICATION_REQUIRED" if challenge else "PROBING",
        "browser_log": browser_log,
        "candidate_count": len(ordered),
        "candidates": [
            {**item, "url": redact_url(item["url"])} for item in ordered
        ],
        "download": None,
        "safe": False,
    }

    if challenge:
        metadata["failure_reason"] = "Source requires human Cloudflare verification before the public player is reachable. No bypass attempted."
        (output_dir / "result.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
        return metadata

    headers = {"User-Agent": user_agent, "Referer": referer, "Accept": "*/*"}
    if cookie_header:
        headers["Cookie"] = cookie_header

    errors: list[dict[str, str]] = []
    title = slugify(str(job.get("title") or "media"))
    base_out = output_dir / f"{title}-ep{requested_episode:03d}.mp4"

    for candidate in ordered:
        url = candidate["url"]
        ct = candidate.get("content_type", "")
        priority = candidate_priority(url, ct)
        if priority <= 10:
            continue
        try:
            if ".m3u8" in url.lower() or "mpegurl" in ct.lower():
                download_hls(url, base_out, headers)
                media_type = "hls-unencrypted"
            else:
                download_direct(url, base_out, headers)
                media_type = "direct-video"
            if base_out.exists() and base_out.stat().st_size > 0:
                verification = probe_media(base_out)
                metadata["download"] = {
                    "file": base_out.name,
                    "bytes": base_out.stat().st_size,
                    "media_type": media_type,
                    "source_url": redact_url(url),
                    "verification": verification,
                }
                metadata["safe"] = bool(verification.get("verified"))
                if metadata["safe"]:
                    metadata["status"] = "SAFE"
                    break
        except Exception as exc:
            errors.append({"url": redact_url(url), "error": f"{type(exc).__name__}: {exc}"})

    # Normal extractor fallback for public sites supported by yt-dlp. No cookies,
    # authenticated browser state, or challenge bypass is provided to the extractor.
    if not metadata["safe"]:
        fallback = ytdlp_download(detail_url, base_out)
        metadata["extractor_fallback"] = {
            "ok": fallback.get("ok", False),
            "error": fallback.get("error"),
        }
        if fallback.get("ok") and fallback.get("file"):
            path = Path(str(fallback["file"]))
            verification = probe_media(path)
            metadata["download"] = {
                "file": path.name,
                "bytes": path.stat().st_size,
                "media_type": "public-extractor",
                "source_url": redact_url(detail_url),
                "verification": verification,
            }
            metadata["safe"] = bool(verification.get("verified"))
            metadata["status"] = "SAFE" if metadata["safe"] else "NOT_PROVEN"

    metadata["errors"] = errors
    if not metadata["safe"] and metadata["status"] == "PROBING":
        metadata["status"] = "NOT_PROVEN"
    (output_dir / "result.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("job", help="Path to job JSON")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    job = json.loads(Path(args.job).read_text(encoding="utf-8"))
    result = asyncio.run(resolve_and_download(job, Path(args.output)))
    if result.get("safe"):
        print(json.dumps(result["download"], indent=2))
        return 0
    print(f"Result: {result.get('status', 'NOT_PROVEN')}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
