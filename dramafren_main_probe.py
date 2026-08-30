#!/usr/bin/env python3
"""Probe DramaFren's public player without bypassing access controls.

The probe only preserves bytes Chromium already received. It does not reconstruct
partial ranges, replay rejected requests, solve challenges, bypass paywalls, or
circumvent DRM. SAFE requires a complete browser-delivered media object that
passes ffprobe verification.
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

from dramafren_browser import candidate_priority, is_media_url, probe_media, redact_url, slugify

MEDIA_TYPES = ("video/", "application/vnd.apple.mpegurl", "application/x-mpegurl")
PLAY_SELECTORS = [
    "button[aria-label*='play' i]",
    "button[title*='play' i]",
    ".vjs-big-play-button",
    ".jw-icon-display",
    ".plyr__control--overlaid",
    "[id*='play' i]",
    "video",
]


def parse_range(value: str | None) -> dict[str, int] | None:
    if not value:
        return None
    m = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", value.strip(), re.I)
    if not m or m.group(3) == "*":
        return None
    return {"start": int(m.group(1)), "end": int(m.group(2)), "total": int(m.group(3))}


def headers_prove_complete(status: int, headers: dict[str, str]) -> bool:
    rng = parse_range(headers.get("content-range"))
    if rng:
        return rng["start"] == 0 and rng["end"] + 1 >= rng["total"]
    return status == 200


async def click_scope(scope: Page | Frame, selector: str) -> bool:
    try:
        loc = scope.locator(selector).first
        if await loc.count() and await loc.is_visible(timeout=1200):
            await loc.click(timeout=4000, force=True)
            return True
    except Exception:
        pass
    return False


async def click_player(page: Page, log: list[dict[str, Any]]) -> bool:
    for scope in [page, *page.frames]:
        for selector in PLAY_SELECTORS:
            if await click_scope(scope, selector):
                log.append({"event": "player-click", "selector": selector, "frame": redact_url(scope.url)})
                return True
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
    timeout_ms = int(job.get("timeout_ms", 90000))
    out_dir.mkdir(parents=True, exist_ok=True)

    candidates: dict[str, dict[str, Any]] = {}
    tasks: list[asyncio.Task[Any]] = []
    log: list[dict[str, Any]] = []
    verified_browser_files: list[dict[str, Any]] = []

    async with async_playwright() as p:
        launch: dict[str, Any] = {
            "headless": True,
            "args": ["--no-sandbox", "--disable-dev-shm-usage", "--autoplay-policy=no-user-gesture-required"],
        }
        if os.environ.get("CHROME_BIN"):
            launch["executable_path"] = os.environ["CHROME_BIN"]

        browser = await p.chromium.launch(**launch)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/136.0.0.0 Safari/537.36",
            viewport={"width": 1365, "height": 900},
            locale="en-US",
        )
        page = await context.new_page()

        async def handle_response(response: Response) -> None:
            try:
                headers = await response.all_headers()
                ctype = headers.get("content-type", "")
                media_url = response.url
                if not (is_media_url(media_url) or any(x in ctype.lower() for x in MEDIA_TYPES)):
                    return

                try:
                    frame_url = response.request.frame.url
                except Exception:
                    frame_url = page.url

                item: dict[str, Any] = {
                    "url": redact_url(media_url),
                    "content_type": ctype,
                    "status": response.status,
                    "source_frame": redact_url(frame_url),
                    "content_length": headers.get("content-length"),
                    "content_range": headers.get("content-range"),
                    "accept_ranges": headers.get("accept-ranges"),
                    "headers_prove_complete": headers_prove_complete(response.status, headers),
                }
                candidates[media_url] = item

                # Crucial rule: do not wait for or reconstruct partial streaming
                # ranges. Only read the body when the response headers themselves
                # already prove this single response contains the entire object.
                if not item["headers_prove_complete"] or not ctype.lower().startswith("video/"):
                    item["browser_body_skipped"] = True
                    return

                try:
                    await asyncio.wait_for(response.finished(), timeout=10)
                    body = await asyncio.wait_for(response.body(), timeout=10)
                except Exception as exc:
                    item["browser_body_error"] = f"{type(exc).__name__}: {exc}"
                    return

                item["browser_body_bytes"] = len(body)
                if not body:
                    return

                path = out_dir / f"browser-complete-{len(verified_browser_files)+1}.mp4"
                path.write_bytes(body)
                verification = probe_media(path)
                item["browser_verification"] = verification
                if verification.get("verified"):
                    verified_browser_files.append({
                        "path": path,
                        "bytes": len(body),
                        "url": media_url,
                        "verification": verification,
                    })
            except Exception as exc:
                log.append({"event": "response-handler-error", "error": f"{type(exc).__name__}: {exc}"})

        def schedule(response: Response) -> None:
            tasks.append(asyncio.create_task(handle_response(response)))

        page.on("response", schedule)

        try:
            initial = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            await page.wait_for_timeout(4000)
            log.append({"event": "goto", "status": initial.status if initial else None, "title": await page.title()})

            await click_player(page, log)
            await page.wait_for_timeout(8000)

            if not any(candidate_priority(raw, c.get("content_type", "")) > 10 for raw, c in candidates.items()):
                for server in ("UPNShare", "Abyss"):
                    if await click_text(page, server):
                        log.append({"event": "server-select", "server": server})
                        await page.wait_for_timeout(3000)
                        await click_player(page, log)
                        await page.wait_for_timeout(8000)
                        if any(candidate_priority(raw, c.get("content_type", "")) > 10 for raw, c in candidates.items()):
                            break

            await page.wait_for_timeout(1500)
            if tasks:
                await asyncio.gather(*list(tasks), return_exceptions=True)

            log.append({
                "event": "final",
                "candidate_count": len(candidates),
                "complete_browser_files": len(verified_browser_files),
                "frames": [redact_url(f.url) for f in page.frames if f.url and f.url != "about:blank"],
            })
            await page.screenshot(path=str(out_dir / "page.png"), full_page=True)
        finally:
            await browser.close()

    result: dict[str, Any] = {
        "job": {"title": job.get("title"), "detail_url": redact_url(url), "episode": episode},
        "status": "NOT_PROVEN",
        "safe": False,
        "candidate_count": len(candidates),
        "candidates": list(candidates.values()),
        "browser_log": log,
        "download": None,
    }

    if verified_browser_files:
        proof = verified_browser_files[0]
        final_name = f"{slugify(str(job.get('title') or 'dramafren'))}-ep{episode:03d}.mp4"
        final_path = out_dir / final_name
        proof["path"].replace(final_path)
        result["safe"] = True
        result["status"] = "SAFE"
        result["download"] = {
            "file": final_name,
            "bytes": proof["bytes"],
            "media_type": "browser-delivered-complete-video",
            "source_url": redact_url(proof["url"]),
            "verification": proof["verification"],
        }

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
