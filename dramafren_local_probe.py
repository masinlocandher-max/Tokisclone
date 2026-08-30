#!/usr/bin/env python3
"""Local assisted probe for publicly exposed DramaFren media.

This deliberately does NOT bypass Cloudflare challenges, CAPTCHAs, authentication,
paywalls, DRM, or encrypted HLS. It opens a normal headed browser on the user's
machine. If the site presents an ordinary browser check, the user may complete it
manually. Once the public player loads, the script observes normal media requests
and can copy direct video or unencrypted HLS to a local file for verification.
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
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright, Response

MEDIA_EXTENSIONS = (".mp4", ".m3u8", ".mpd", ".webm")
MEDIA_CONTENT_TYPES = (
    "video/",
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "application/dash+xml",
)


def slugify(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-")
    return value[:120] or "dramafren"


def sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def ffprobe(path: Path) -> dict[str, Any]:
    if not shutil.which("ffprobe"):
        return {"available": False}
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_streams",
            "-show_entries", "format=duration,size,format_name",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return {"available": True, "error": proc.stderr.strip()}
    return {"available": True, "data": json.loads(proc.stdout)}


def is_media(url: str, content_type: str = "") -> bool:
    path = urllib.parse.urlparse(url).path.lower()
    ct = (content_type or "").lower()
    return any(path.endswith(ext) for ext in MEDIA_EXTENSIONS) or any(
        hint in ct for hint in MEDIA_CONTENT_TYPES
    )


def priority(url: str, content_type: str = "") -> int:
    low = url.lower()
    ct = (content_type or "").lower()
    if ".mp4" in low or "video/mp4" in ct:
        return 100
    if ".webm" in low or "video/webm" in ct:
        return 95
    if ".m3u8" in low or "mpegurl" in ct:
        return 80
    if ".mpd" in low or "dash+xml" in ct:
        return 10
    if ct.startswith("video/"):
        return 60
    return 0


def download_direct(url: str, out: Path, headers: dict[str, str]) -> None:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=120) as resp, out.open("wb") as fh:
        shutil.copyfileobj(resp, fh)


def fetch_text(url: str, headers: dict[str, str]) -> str:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


def download_hls(url: str, out: Path, headers: dict[str, str]) -> None:
    manifest = fetch_text(url, headers)
    if "#EXT-X-KEY" in manifest.upper():
        raise RuntimeError("Encrypted HLS detected; refusing to process")
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required for HLS")
    header_blob = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
            "-headers", header_blob, "-i", url, "-c", "copy", str(out),
        ],
        check=True,
        timeout=1800,
    )


async def run(args: argparse.Namespace) -> dict[str, Any]:
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    profile_dir = Path(args.profile_dir).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)

    candidates: dict[str, dict[str, Any]] = {}
    log: list[dict[str, Any]] = []

    async with async_playwright() as p:
        launch_args = [
            "--disable-dev-shm-usage",
            "--autoplay-policy=no-user-gesture-required",
        ]
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            viewport={"width": 1365, "height": 900},
            locale="en-US",
            args=launch_args,
        )
        page = context.pages[0] if context.pages else await context.new_page()

        async def on_response(response: Response) -> None:
            try:
                ct = response.headers.get("content-type", "")
                if is_media(response.url, ct):
                    candidates[response.url] = {
                        "url": response.url,
                        "status": response.status,
                        "content_type": ct,
                        "source": "network-response",
                    }
            except Exception:
                pass

        page.on("response", on_response)

        resp = await page.goto(args.url, wait_until="domcontentloaded", timeout=90000)
        log.append({
            "event": "goto",
            "status": resp.status if resp else None,
            "url": page.url,
            "title": await page.title(),
        })

        print("\nBrowser opened.")
        print("If DramaFren shows an ordinary browser check, complete it manually.")
        print("Then open episode 1 and press Play in the browser.")
        print(f"The probe will observe traffic for up to {args.observe_seconds} seconds.")
        print("No CAPTCHA, Cloudflare, DRM, login, or paywall bypass is attempted.\n")

        deadline = time.monotonic() + args.observe_seconds
        while time.monotonic() < deadline:
            await page.wait_for_timeout(1000)
            title = await page.title()
            url = page.url
            if title != "Just a moment...":
                log.append({"event": "page-accessible", "url": url, "title": title})
            if candidates:
                # Keep observing briefly so a master playlist/direct MP4 can win ranking.
                if time.monotonic() + 8 < deadline:
                    await page.wait_for_timeout(8000)
                break

        try:
            dom = await page.eval_on_selector_all(
                "video, video source, source",
                "els => els.map(e => e.currentSrc || e.src || e.getAttribute('src')).filter(Boolean)",
            )
            for media_url in dom:
                absolute = urllib.parse.urljoin(page.url, media_url)
                candidates.setdefault(absolute, {
                    "url": absolute,
                    "status": None,
                    "content_type": "",
                    "source": "dom",
                })
        except Exception:
            pass

        try:
            await page.screenshot(path=str(output / "page.png"), full_page=True)
        except Exception:
            pass

        cookies = await context.cookies()
        cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        user_agent = await page.evaluate("navigator.userAgent")
        referer = page.url
        final_title = await page.title()
        final_url = page.url
        await context.close()

    ordered = sorted(
        candidates.values(),
        key=lambda item: priority(item["url"], item.get("content_type", "")),
        reverse=True,
    )

    report: dict[str, Any] = {
        "target": args.url,
        "final_url": final_url,
        "final_title": final_title,
        "browser_log": log,
        "candidate_count": len(ordered),
        "candidates": ordered,
        "download": None,
        "verification": None,
        "safe": False,
        "errors": [],
    }

    headers = {
        "User-Agent": user_agent,
        "Referer": referer,
        "Accept": "*/*",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header

    for candidate in ordered:
        media_url = candidate["url"]
        ct = candidate.get("content_type", "")
        if priority(media_url, ct) <= 10:
            continue
        try:
            out = output / f"{slugify(args.title)}-ep{args.episode:03d}.mp4"
            if ".m3u8" in media_url.lower() or "mpegurl" in ct.lower():
                download_hls(media_url, out, headers)
                media_type = "hls-unencrypted"
            else:
                download_direct(media_url, out, headers)
                media_type = "direct-video"
            if out.exists() and out.stat().st_size > 0:
                probe = ffprobe(out)
                valid_video = False
                if probe.get("data"):
                    streams = probe["data"].get("streams", [])
                    valid_video = any(s.get("codec_type") == "video" for s in streams)
                report["download"] = {
                    "file": out.name,
                    "bytes": out.stat().st_size,
                    "media_type": media_type,
                    "source_url": media_url,
                }
                report["verification"] = {
                    "sha256": sha256(out),
                    "ffprobe": probe,
                    "has_video_stream": valid_video,
                }
                report["safe"] = bool(valid_video)
                break
        except Exception as exc:
            report["errors"].append({"url": media_url, "error": repr(exc)})

    (output / "result.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--title", default="Here Comes the Straight-A Mafia Boss")
    ap.add_argument("--episode", type=int, default=1)
    ap.add_argument("--output", default="dramafren-local-output")
    ap.add_argument("--profile-dir", default=".dramafren-browser-profile")
    ap.add_argument("--observe-seconds", type=int, default=180)
    args = ap.parse_args()

    result = asyncio.run(run(args))
    print(json.dumps({
        "safe": result["safe"],
        "candidate_count": result["candidate_count"],
        "download": result["download"],
        "final_title": result["final_title"],
    }, indent=2))
    return 0 if result["safe"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
