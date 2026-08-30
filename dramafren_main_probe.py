#!/usr/bin/env python3
"""Probe DramaFren's normally accessible player and preserve public media bytes.

No CAPTCHA, Cloudflare, authentication, paywall, DRM, encryption, or access-
control bypass is attempted. The preferred proof path saves only bytes Chromium
already received while playing the public page. SAFE is returned only when the
browser-delivered response is the complete media object and ffprobe verifies it,
or when a normal direct public request succeeds independently.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
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
    return {k: v for k, v in raw.items() if k.lower() in FORWARDABLE_HEADERS}


def parse_content_range(value: str | None) -> dict[str, int] | None:
    if not value:
        return None
    match = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", value.strip(), re.I)
    if not match or match.group(3) == "*":
        return None
    start, end, total = (int(match.group(i)) for i in (1, 2, 3))
    return {"start": start, "end": end, "total": total}


def response_is_complete(status: int, headers: dict[str, str], body_len: int) -> bool:
    content_range = parse_content_range(headers.get("content-range"))
    if content_range:
        return (
            content_range["start"] == 0
            and content_range["end"] + 1 >= content_range["total"]
            and body_len >= content_range["total"]
        )
    if status == 200:
        try:
            declared = int(headers.get("content-length", "0"))
        except ValueError:
            declared = 0
        return declared == 0 or body_len >= declared
    return False


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
    response_tasks: list[asyncio.Task[Any]] = []
    browser_complete_files: list[dict[str, Any]] = []

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

        async def handle_response(response: Response) -> None:
            try:
                headers = await response.all_headers()
                content_type = headers.get("content-type", "")
                media_url = response.url
                if not (
                    is_media_url(media_url)
                    or any(x in content_type.lower() for x in MEDIA_CONTENT_TYPES)
                ):
                    return

                request_headers = await response.request.all_headers()
                try:
                    source_frame = response.request.frame.url
                except Exception:
                    source_frame = page.url

                item: dict[str, Any] = {
                    "url": media_url,
                    "content_type": content_type,
                    "status": str(response.status),
                    "source": "network-response",
                    "source_frame": redact_url(source_frame),
                    "content_length": headers.get("content-length"),
                    "content_range": headers.get("content-range"),
                    "accept_ranges": headers.get("accept-ranges"),
                    "_request_headers": safe_forward_headers(request_headers),
                }
                candidates[media_url] = item

                # Preferred proof: save exactly the bytes Chromium already
                # received. Never issue extra range requests to complete it.
                if content_type.lower().startswith("video/") and response.status in (200, 206):
                    try:
                        await response.finished()
                        body = await response.body()
                        item["browser_body_bytes"] = len(body)
                        complete = response_is_complete(response.status, headers, len(body))
                        item["browser_body_complete"] = complete
                        if complete and body:
                            path = out_dir / f"browser-delivered-{len(browser_complete_files)+1}.mp4"
                            path.write_bytes(body)
                            verification = probe_media(path)
                            item["browser_verification"] = verification
                            if verification.get("verified"):
                                browser_complete_files.append({
                                    "path": path,
                                    "url": media_url,
                                    "verification": verification,
                                    "bytes": len(body),
                                })
                    except Exception as exc:
                        item["browser_body_error"] = f"{type(exc).__name__}: {exc}"
            except Exception as exc:
                log.append({"event": "response-handler-error", "error": f"{type(exc).__name__}: {exc}"})

        def schedule_response(response: Response) -> None:
            response_tasks.append(asyncio.create_task(handle_response(response)))

        page.on("response", schedule_response)

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
            log.append({"event": "frames-before-play", "count": len(before_frames), "frames": before_frames[:20]})

            clicked = await click_player_controls(page, log)
            await page.wait_for_timeout(8000)

            if not any(candidate_priority(c["url"], c.get("content_type", "")) > 10 for c in candidates.values()):
                for server in ("UPNShare", "Abyss"):
                    if await click_text(page, server):
                        log.append({"event": "server-select", "server": server})
                        await page.wait_for_timeout(4000)
                        await click_player_controls(page, log)
                        await page.wait_for_timeout(10000)
                        if any(candidate_priority(c["url"], c.get("content_type", "")) > 10 for c in candidates.values()):
                            break

            # Give media response handlers a chance to finish while Chromium is
            # still open. They only read responses that the player already made.
            await page.wait_for_timeout(3000)
            if response_tasks:
                await asyncio.gather(*list(response_tasks), return_exceptions=True)

            after_frames = [
                redact_url(frame.url)
                for frame in page.frames
                if frame.url and frame.url != "about:blank"
            ]
            log.append({"event": "frames-after-play", "count": len(after_frames), "frames": after_frames[:20]})
            log.append({
                "event": "final",
                "candidate_count": len(candidates),
                "clicked": clicked,
                "browser_complete_file_count": len(browser_complete_files),
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

    public_candidates: list[dict[str, Any]] = []
    for candidate in ordered:
        public_candidates.append({
            key: (redact_url(value) if key == "url" else value)
            for key, value in candidate.items()
            if not key.startswith("_")
        })

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

    # If Chromium itself received the whole object, that is sufficient proof.
    if browser_complete_files:
        proof = browser_complete_files[0]
        final_name = f"{slugify(str(job.get('title') or 'dramafren'))}-ep{episode:03d}.mp4"
        final_path = out_dir / final_name
        proof["path"].replace(final_path)
        result["download"] = {
            "file": final_name,
            "bytes": proof["bytes"],
            "media_type": "browser-delivered-direct-video",
            "source_url": redact_url(proof["url"]),
            "verification": proof["verification"],
        }
        result["safe"] = True
        result["status"] = "SAFE"
    else:
        # Secondary path for genuinely public media that permits a normal
        # independent request. If the origin says 403, record it and stop.
        base = out_dir / f"{slugify(str(job.get('title') or 'dramafren'))}-ep{episode:03d}.mp4"
        for candidate in ordered:
            media_url = candidate["url"]
            content_type = candidate.get("content_type", "")
            if candidate_priority(media_url, content_type) <= 10:
                continue

            captured_headers = dict(candidate.get("_request_headers") or {})
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

    (out_dir / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
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

    print(f"Result: {result.get('status')}; candidates={result.get('candidate_count')}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
