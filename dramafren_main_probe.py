#!/usr/bin/env python3
"""Probe DramaFren main-site embedded players for public, non-DRM media.

This script does not bypass Cloudflare, CAPTCHA, authentication, paywalls, DRM,
or encryption. It only interacts with visible player controls on a normally
loaded public page, then verifies any copied media independently.
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
    # First try known player selectors in the main page and all child frames.
    scopes: list[Page | Frame] = [page, *page.frames]
    for scope in scopes:
        for selector in COMMON_PLAY_SELECTORS:
            if await click_selector_in_scope(scope, selector):
                log.append({"event": "player-click", "selector": selector, "frame": redact_url(scope.url)})
                return True

    # Then click the center of a visible iframe if present. This is equivalent
    # to the user clicking the displayed embedded player, not a challenge.
    try:
        frames = page.locator("iframe")
        count = await frames.count()
        best = None
        best_area = 0.0
        for i in range(count):
            box = await frames.nth(i).bounding_box()
            if box:
                area = float(box["width"] * box["height"])
                if area > best_area:
                    best_area = area
                    best = box
        if best and best_area > 40000:
            x = best["x"] + best["width"] / 2
            y = best["y"] + best["height"] / 2
            await page.mouse.click(x, y)
            log.append({"event": "player-click", "selector": "largest-iframe-center", "area": int(best_area)})
            return True
    except Exception:
        pass

    # Finally find a large visible black/player-like element in the upper page
    # and click its center. This only acts on the already-rendered public page.
    try:
        box = await page.evaluate("""
        () => {
          const els = Array.from(document.querySelectorAll('video, iframe, [class*=player], [id*=player], [class*=video]'));
          const candidates = els.map(el => {
            const r = el.getBoundingClientRect();
            return {x:r.x,y:r.y,w:r.width,h:r.height,area:r.width*r.height};
          }).filter(x => x.area > 40000 && x.y < 900 && x.w > 300 && x.h > 180)
            .sort((a,b) => b.area-a.area);
          return candidates[0] || null;
        }
        """)
        if box:
            await page.mouse.click(box["x"] + box["w"] / 2, box["y"] + box["h"] / 2)
            log.append({"event": "player-click", "selector": "largest-player-center", "area": int(box["area"])})
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

    candidates: dict[str, dict[str, str]] = {}
    log: list[dict[str, Any]] = []
    frame_urls: list[str] = []
    user_agent = "Mozilla/5.0"
    referer = url
    cookie_header = ""

    async with async_playwright() as p:
        launch: dict[str, Any] = {
            "headless": True,
            "args": ["--no-sandbox", "--disable-dev-shm-usage", "--autoplay-policy=no-user-gesture-required"],
        }
        if os.environ.get("CHROME_BIN"):
            launch["executable_path"] = os.environ["CHROME_BIN"]
        browser = await p.chromium.launch(**launch)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1365, "height": 900},
            locale="en-US",
        )
        page = await context.new_page()

        async def on_response(response: Response) -> None:
            try:
                ct = response.headers.get("content-type", "")
                media_url = response.url
                if is_media_url(media_url) or any(x in ct.lower() for x in MEDIA_CONTENT_TYPES):
                    candidates[media_url] = {
                        "url": media_url,
                        "content_type": ct,
                        "status": str(response.status),
                        "source": "network-response",
                    }
            except Exception:
                pass

        page.on("response", on_response)

        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            await page.wait_for_timeout(4000)
            title = await page.title()
            log.append({"event": "goto", "status": response.status if response else None, "title": title, "url": redact_url(page.url)})

            # Record iframe topology without persisting query values.
            for frame in page.frames:
                if frame.url and frame.url != "about:blank":
                    frame_urls.append(redact_url(frame.url))
            log.append({"event": "frames-before-play", "count": len(frame_urls), "frames": frame_urls[:20]})

            # Try the visible player directly.
            clicked = await click_player_controls(page, log)
            await page.wait_for_timeout(8000)

            # DramaFren visibly offers alternate player buttons. If the first
            # player yielded nothing, switch using the UI exactly as a user can.
            if not candidates:
                for server in ("UPNShare", "Abyss"):
                    if await click_text(page, server):
                        log.append({"event": "server-select", "server": server})
                        await page.wait_for_timeout(4000)
                        await click_player_controls(page, log)
                        await page.wait_for_timeout(8000)
                        if candidates:
                            break

            # DOM and resource entries may expose a direct public media URL.
            try:
                sources = await page.eval_on_selector_all(
                    "video, video source, source",
                    "els => els.map(e => e.currentSrc || e.src || e.getAttribute('src')).filter(Boolean)",
                )
                for media_url in sources:
                    absolute = urllib.parse.urljoin(page.url, media_url)
                    if is_media_url(absolute):
                        candidates.setdefault(absolute, {"url": absolute, "content_type": "", "status": "", "source": "dom"})
            except Exception:
                pass

            try:
                resources = await page.evaluate("performance.getEntriesByType('resource').map(e => e.name)")
                for media_url in resources:
                    if is_media_url(media_url):
                        candidates.setdefault(media_url, {"url": media_url, "content_type": "", "status": "", "source": "performance"})
            except Exception:
                pass

            # Re-record frame URLs after player/server interaction.
            after_frames = []
            for frame in page.frames:
                if frame.url and frame.url != "about:blank":
                    after_frames.append(redact_url(frame.url))
            log.append({"event": "frames-after-play", "count": len(after_frames), "frames": after_frames[:20]})
            log.append({"event": "final", "candidate_count": len(candidates), "clicked": clicked, "title": await page.title()})
            await page.screenshot(path=str(out_dir / "page.png"), full_page=True)

            cookies = await context.cookies()
            cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            user_agent = await page.evaluate("navigator.userAgent")
            referer = page.url
        finally:
            await browser.close()

    ordered = sorted(candidates.values(), key=lambda c: candidate_priority(c["url"], c.get("content_type", "")), reverse=True)
    public_candidates = [{**c, "url": redact_url(c["url"])} for c in ordered]
    result: dict[str, Any] = {
        "job": {"title": job.get("title"), "detail_url": redact_url(url), "episode": episode},
        "status": "NOT_PROVEN",
        "browser_log": log,
        "candidate_count": len(ordered),
        "candidates": public_candidates,
        "download": None,
        "safe": False,
        "errors": [],
    }

    headers = {"User-Agent": user_agent, "Referer": referer, "Accept": "*/*"}
    if cookie_header:
        headers["Cookie"] = cookie_header

    base = out_dir / f"{slugify(str(job.get('title') or 'dramafren'))}-ep{episode:03d}.mp4"
    for candidate in ordered:
        media_url = candidate["url"]
        ct = candidate.get("content_type", "")
        if candidate_priority(media_url, ct) <= 10:
            continue
        try:
            if ".m3u8" in media_url.lower() or "mpegurl" in ct.lower():
                download_hls(media_url, base, headers)
                media_type = "hls-unencrypted"
            else:
                download_direct(media_url, base, headers)
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
            result["errors"].append({"url": redact_url(media_url), "error": f"{type(exc).__name__}: {exc}"})

    (out_dir / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("job")
    ap.add_argument("--output", required=True)
    args = ap.parse_args()
    job = json.loads(Path(args.job).read_text(encoding="utf-8"))
    result = asyncio.run(run(job, Path(args.output)))
    if result.get("safe"):
        print(json.dumps(result["download"], indent=2))
        return 0
    print(f"Result: {result.get('status')}; candidates={result.get('candidate_count')}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
