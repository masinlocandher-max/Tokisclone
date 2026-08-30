#!/usr/bin/env python3
"""Resolve and download publicly exposed DramaFren episode media.

This worker intentionally does not bypass authentication, CAPTCHA, DRM, encrypted
HLS, or paywalls. It only captures media URLs exposed to a normal public browser
session and downloads direct MP4 or unencrypted HLS streams.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
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
    return value[:100] or "dramafren"


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
        return 10  # inspected but not downloaded; DASH may be DRM-protected
    if ct.startswith("video/"):
        return 60
    return 0


def download_direct(url: str, out: Path, headers: dict[str, str]) -> None:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=90) as response, out.open("wb") as fh:
        shutil.copyfileobj(response, fh)


def fetch_text(url: str, headers: dict[str, str]) -> str:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=45) as response:
        return response.read().decode("utf-8", errors="replace")


def download_hls(url: str, out: Path, headers: dict[str, str]) -> None:
    manifest = fetch_text(url, headers)
    # Do not process encrypted HLS. This is a deliberate access-control boundary.
    if "#EXT-X-KEY" in manifest.upper():
        raise RuntimeError("Encrypted HLS detected (#EXT-X-KEY); refusing to bypass encryption")
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required for HLS but was not found")
    header_blob = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        "-headers", header_blob,
        "-i", url,
        "-c", "copy",
        str(out),
    ]
    subprocess.run(cmd, check=True, timeout=600)


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
    detail_url = job["detail_url"]
    requested_episode = int(job.get("episode", 1))
    timeout_ms = int(job.get("timeout_ms", 45000))
    output_dir.mkdir(parents=True, exist_ok=True)

    candidates: dict[str, dict[str, str]] = {}
    browser_log: list[dict[str, Any]] = []

    async with async_playwright() as p:
        launch_kwargs: dict[str, Any] = {
            "headless": True,
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
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
            browser_log.append({
                "event": "goto",
                "url": page.url,
                "status": response.status if response else None,
                "title": await page.title(),
            })
            await page.wait_for_timeout(3000)

            # Try explicit requested episode before falling back to Start Watching.
            ep_patterns = [f"Ep {requested_episode}", f"EP {requested_episode}", f"Episode {requested_episode}"]
            clicked = await click_first_visible(page, ep_patterns)
            if not clicked:
                clicked = await click_first_visible(page, ["Start Watching", "Watch Now", "Play Now"])
            browser_log.append({"event": "initial-click", "clicked": clicked, "url": page.url})
            await page.wait_for_timeout(5000)

            # A detail click may navigate to watch page. Probe common play controls there.
            await click_first_visible(page, ["Play", "Start", "Resume"])
            try:
                await page.locator("video").first.click(timeout=2500)
            except Exception:
                pass
            await page.wait_for_timeout(10000)

            # Collect direct DOM sources as well as browser performance resources.
            dom_sources = await page.eval_on_selector_all(
                "video, video source, source",
                "els => els.map(e => e.currentSrc || e.src || e.getAttribute('src')).filter(Boolean)",
            )
            for url in dom_sources:
                absolute = urllib.parse.urljoin(page.url, url)
                candidates.setdefault(absolute, {
                    "url": absolute,
                    "content_type": "",
                    "status": "",
                    "source": "dom",
                })

            perf_urls = await page.evaluate(
                "performance.getEntriesByType('resource').map(e => e.name)"
            )
            for url in perf_urls:
                if is_media_url(url):
                    candidates.setdefault(url, {
                        "url": url,
                        "content_type": "",
                        "status": "",
                        "source": "performance",
                    })

            browser_log.append({"event": "final", "url": page.url, "title": await page.title()})
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

    metadata = {
        "job": job,
        "browser_log": browser_log,
        "candidate_count": len(ordered),
        "candidates": ordered,
        "download": None,
    }

    headers = {
        "User-Agent": user_agent,
        "Referer": referer,
        "Accept": "*/*",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header

    errors: list[dict[str, str]] = []
    title = slugify(job.get("title") or "dramafren")
    for candidate in ordered:
        url = candidate["url"]
        ct = candidate.get("content_type", "")
        priority = candidate_priority(url, ct)
        if priority <= 10:  # e.g. DASH/MPD: report only, do not process.
            continue
        try:
            lower = url.lower()
            if ".m3u8" in lower or "mpegurl" in ct.lower():
                out = output_dir / f"{title}-ep{requested_episode:03d}.mp4"
                download_hls(url, out, headers)
                media_type = "hls-unencrypted"
            else:
                out = output_dir / f"{title}-ep{requested_episode:03d}.mp4"
                download_direct(url, out, headers)
                media_type = "direct-video"
            if out.exists() and out.stat().st_size > 0:
                metadata["download"] = {
                    "file": out.name,
                    "bytes": out.stat().st_size,
                    "media_type": media_type,
                    "source_url": url,
                }
                break
        except Exception as exc:
            errors.append({"url": url, "error": repr(exc)})

    metadata["errors"] = errors
    (output_dir / "result.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("job", help="Path to job JSON")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    job = json.loads(Path(args.job).read_text(encoding="utf-8"))
    result = asyncio.run(resolve_and_download(job, Path(args.output)))
    if result.get("download"):
        print(json.dumps(result["download"], indent=2))
        return 0
    print("No downloadable public unencrypted media was resolved.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
