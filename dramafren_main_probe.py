#!/usr/bin/env python3
"""Probe DramaFren's normally accessible player and copy public non-DRM media.

No CAPTCHA, Cloudflare, authentication, paywall, DRM, or encryption bypass is
attempted. SAFE is returned only after a local media file is copied and passes
ffprobe verification.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright, Page, Frame, Response

from dramafren_browser import (
    candidate_priority,
    download_direct,
    download_hls,
    is_media_url,
    probe_media,
    redact_url,
    slugify,
)

MEDIA_CONTENT_TYPES = (
    "video/",
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "application/dash+xml",
)

COMMON_PLAY_SELECTORS = [
    "button[aria-label*='play' i]",
    "button[title*='play' i]",
    ".vjs-big-play-button",
    ".jw-icon-display",
    ".plyr__control--overlaid",
    ".mejs__overlay-button",
    "[class*='play-button' i]",
    "[class*='playButton' i]",
    "[id*='play' i]",
    "video",
]

# Never persist or forward credentials. These are ordinary browser request
# headers needed for public hotlink/referrer/range behavior only.
FORWARDABLE_HEADERS = {
    "accept",
    "accept-language",
    "cache-control",
    "origin",
    "pragma",
    "range",
    "referer",
    "sec-fetch-dest",
    "sec-fetch-mode",
    "sec-fetch-site",
    "user-agent",
}


def safe_forward_headers(raw: dict[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in raw.items()
        if key.lower() in FORWARDABLE_HEADERS
    }


async def click_selector_in_scope(scope: Page | Frame, selector: str) -> bool:
    try:
        loc = scope.locator(selector).first
        if await loc.count() and await loc.is_visible(timeout=1200):
            await loc.click(timeout=4000, force=True)
            return True
    except Exception:
        return False
    return False


async def click_player_controls(page: Page, log: list[dict[str, Any]]) -> bool:
    for scope in [page, *page.frames]:
        for selector in COMMON_PLAY_SELECTORS:
            if await click_selector_in_scope(scope, selector):
                log.append({
                    "event": "player-click",
                    "selector": selector,
                    "frame": redact_url(scope.url),
                })
                return True

    # Clicking the center of a visible embedded player is ordinary browser
    # interaction, not an attempt to solve or evade a verification challenge.
    try:
        frames = page.locator("iframe")
        best = None
        best_area = 0.0
        for i in range(await frames.count()):
            box = await frames.nth(i).bounding_box()
            if box:
                area = float(box["width"] * box["height"])
                if area > best_area:
                    best_area = area
                    best = box
        if best and best_area > 40000:
            await page.mouse.click(
                best["x"] + best["width"] / 2,
                best["y"] + best["height"] / 2,
            )
            log.append({"event": "player-click", "selector": "largest-iframe-center"})
            return True
    except Exception:
        pass
    return False


async def click_text(page: Page, text: str) -> bool:
    try:
        loc = page.get_by_text(re.compile(rf"^\s*{re.escape(text)}\s*$", re.I)).first
        if await loc.count() and await loc.is_visible(timeout=1200):
            await loc.click(timeout=4000, force=True)
            return True
    except Exception:
        pass
    return False


async def run(job: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    url = str(job["detail_url"])
    episode = int(job.get("episode", 1))
    timeout_ms = int(job.get("timeout_ms", 60000))
    out_dir.mkdir(parents=True, exist_ok=True)

    candidates: dict[str, dict[str, Any]] = {}
    log: list[dict[str, Any]] = []

    async with async_playwright() as p:
        launch: dict[str, Any] = {
            "headless": True,
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--autoplay-policy=no-user-gesture-required",
            ],
        }
        if os.environ.get("CHROME_BIN"):
            launch["executable_path"] = os.environ["CHROME_BIN"]

        browser = await p.chromium.launch(**launch)
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
                content_type = response.headers.get("content-type", "")
                media_url = response.url
                if not (
                    is_media_url(media_url)
                    or any(x in content_type.lower() for x in MEDIA_CONTENT_TYPES)
                ):
                    return
                request_headers = await response.request.all_headers()
                source_frame = response.request.frame.url
                candidates[media_url] = {
                    "url": media_url,
                    "content_type": content_type,
                    "status": str(response.status),
                    "source": "network-response",
                    "source_frame": redact_url(source_frame),
                    # Private runtime-only field. It is stripped from result.json.
                    "_request_headers": safe_forward_headers(request_headers),
                }
            except Exception:
                pass

        page.on("response", on_response)

        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            await page.wait_for_timeout(4000)
            log.append({
                "event": "goto",
                "status": response.status if response else None,
                "title": await page.title(),
                "url": redact_url(page.url),
            })

            before_frames = [
                redact_url(frame.url)
                for frame in page.frames
                if frame.url and frame.url != "about:blank"
            ]
            log.append({
                "event": "frames-before-play",
                "count": len(before_frames),
                "frames": before_frames[:20],
            })

            clicked = await click_player_controls(page, log)
            await page.wait_for_timeout(8000)

            if not candidates:
                for server in ("UPNShare", "Abyss"):
                    if await click_text(page, server):
                        log.append({"event": "server-select", "server": server})
                        await page.wait_for_timeout(4000)
                        await click_player_controls(page, log)
                        await page.wait_for_timeout(8000)
                        if candidates:
                            break

            try:
                resources = await page.evaluate(
                    "performance.getEntriesByType('resource').map(e => e.name)"
                )
                for media_url in resources:
                    if is_media_url(media_url):
                        candidates.setdefault(media_url, {
                            "url": media_url,
                            "content_type": "",
                            "status": "",
                            "source": "performance",
                            "source_frame": redact_url(page.url),
                            "_request_headers": {},
                        })
            except Exception:
                pass

            after_frames = [
                redact_url(frame.url)
                for frame in page.frames
                if frame.url and frame.url != "about:blank"
            ]
            log.append({
                "event": "frames-after-play",
                "count": len(after_frames),
                "frames": after_frames[:20],
            })
            log.append({
                "event": "final",
                "candidate_count": len(candidates),
                "clicked": clicked,
                "title": await page.title(),
            })
            await page.screenshot(path=str(out_dir / "page.png"), full_page=True)
        finally:
            await browser.close()

    ordered = sorted(
        candidates.values(),
        key=lambda item: candidate_priority(item["url"], item.get("content_type", "")),
        reverse=True,
    )

    public_candidates = []
    for candidate in ordered:
        public_candidates.append({
            key: (redact_url(value) if key == "url" else value)
            for key, value in candidate.items()
            if not key.startswith("_")
        })

    result: dict[str, Any] = {
        "job": {
            "title": job.get("title"),
            "detail_url": redact_url(url),
            "episode": episode,
        },
        "status": "NOT_PROVEN",
        "browser_log": log,
        "candidate_count": len(ordered),
        "candidates": public_candidates,
        "download": None,
        "safe": False,
        "errors": [],
    }

    base = out_dir / f"{slugify(str(job.get('title') or 'dramafren'))}-ep{episode:03d}.mp4"

    for candidate in ordered:
        media_url = candidate["url"]
        content_type = candidate.get("content_type", "")
        if candidate_priority(media_url, content_type) <= 10:
            continue

        captured_headers = dict(candidate.get("_request_headers") or {})
        # For a browser media element, Range: bytes=0- is normal and may be
        # required by the origin. It still retrieves the full resource to EOF.
        captured_headers.setdefault("range", "bytes=0-")
        captured_headers.setdefault("accept", "*/*")
        if not captured_headers.get("referer") and candidate.get("source_frame"):
            captured_headers["referer"] = str(candidate["source_frame"])

        try:
            if ".m3u8" in media_url.lower() or "mpegurl" in content_type.lower():
                download_hls(media_url, base, captured_headers)
                media_type = "hls-unencrypted"
            else:
                download_direct(media_url, base, captured_headers)
                media_type = "direct-video"

            if base.exists() and base.stat().st_size > 0:
                verification = probe_media(base)
                result["download"] = {
                    "file": base.name,
                    "bytes": base.stat().st_size,
                    "media_type": media_type,
                    "source_url": redact_url(media_url),
                    "verification": verification,
                }
                result["safe"] = bool(verification.get("verified"))
                result["status"] = "SAFE" if result["safe"] else "NOT_PROVEN"
                if result["safe"]:
                    break
        except Exception as exc:
            result["errors"].append({
                "url": redact_url(media_url),
                "source_frame": candidate.get("source_frame"),
                "error": f"{type(exc).__name__}: {exc}",
            })

    (out_dir / "result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("job")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    job = json.loads(Path(args.job).read_text(encoding="utf-8"))
    result = asyncio.run(run(job, Path(args.output)))
    if result.get("safe"):
        print(json.dumps(result["download"], indent=2))
        return 0

    print(
        f"Result: {result.get('status')}; candidates={result.get('candidate_count')}",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
