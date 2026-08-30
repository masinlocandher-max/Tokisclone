#!/usr/bin/env python3
"""Local DramaFren -> Google Drive worker.

Runs on the owner's computer with a persistent, headed Chrome profile. If
Cloudflare asks for human verification, the owner completes it manually in the
opened browser. The worker never clicks/bypasses CAPTCHAs and refuses encrypted
HLS (#EXT-X-KEY) or DASH/DRM-like delivery.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from playwright.async_api import Page, Response, async_playwright

from drive_storage import DriveStorage

ALLOWED_HOST = "dramabox.dramafren.org"
MEDIA_EXTENSIONS = (".mp4", ".m3u8")
MEDIA_CONTENT_TYPES = (
    "video/",
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
)


def safe_name(value: str, fallback: str = "unknown") -> str:
    value = re.sub(r"[\\/:*?\"<>|]+", "-", (value or fallback).strip())
    value = re.sub(r"\s+", " ", value).strip(" .-")
    return value[:140] or fallback


def validate_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").lower() != ALLOWED_HOST:
        raise ValueError(f"Only public https://{ALLOWED_HOST}/ URLs are accepted.")
    return value.strip()


def media_priority(url: str, content_type: str = "") -> int:
    u = url.lower()
    ct = content_type.lower()
    if ".mp4" in u or ct.startswith("video/mp4"):
        return 100
    if ".m3u8" in u or "mpegurl" in ct:
        return 80
    if ct.startswith("video/"):
        return 60
    return 0


def fetch_text(url: str, headers: dict[str, str]) -> str:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as response:
        return response.read().decode("utf-8", errors="replace")


def download_direct(url: str, out: Path, headers: dict[str, str]) -> None:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=120) as response, out.open("wb") as fh:
        shutil.copyfileobj(response, fh)


def download_hls(url: str, out: Path, headers: dict[str, str]) -> None:
    manifest = fetch_text(url, headers)
    if "#EXT-X-KEY" in manifest.upper():
        raise RuntimeError("Encrypted HLS detected. This worker will not bypass encryption.")
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required for HLS downloads.")
    header_blob = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
            "-headers", header_blob, "-i", url, "-c", "copy", str(out),
        ],
        check=True,
        timeout=1800,
    )


async def wait_for_manual_verification(page: Page, timeout_seconds: int) -> None:
    title = await page.title()
    body = ""
    try:
        body = (await page.locator("body").inner_text(timeout=2000))[:1000]
    except Exception:
        pass
    challenged = "just a moment" in title.lower() or "verify you are human" in body.lower()
    if not challenged:
        return

    print("\nDramaFren requested Cloudflare verification.")
    print("Complete the human verification manually in the Chrome window.")
    print("The worker will continue automatically after the real page appears.\n")

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        await page.wait_for_timeout(1000)
        try:
            title = await page.title()
            body = (await page.locator("body").inner_text(timeout=1500))[:1000]
        except Exception:
            continue
        if "just a moment" not in title.lower() and "verify you are human" not in body.lower():
            await page.wait_for_timeout(1500)
            return
    raise RuntimeError("Human verification was not completed before the timeout.")


async def click_play_controls(page: Page, episode: int) -> None:
    labels = [
        f"Episode {episode}", f"Ep {episode}", f"EP {episode}",
        "Start Watching", "Watch Now", "Play Now", "Play", "Resume",
    ]
    for label in labels:
        for locator in (
            page.get_by_role("button", name=re.compile(re.escape(label), re.I)),
            page.get_by_role("link", name=re.compile(re.escape(label), re.I)),
            page.get_by_text(re.compile(rf"^\s*{re.escape(label)}\s*$", re.I)),
        ):
            try:
                if await locator.count() and await locator.first.is_visible(timeout=800):
                    await locator.first.click(timeout=4000)
                    await page.wait_for_timeout(1800)
                    break
            except Exception:
                pass

    try:
        video = page.locator("video").first
        if await video.count():
            await video.evaluate("v => { v.muted = true; const p=v.play(); if(p) p.catch(()=>{}); }")
    except Exception:
        pass


async def resolve_episode(job: dict[str, Any], output_dir: Path, profile_dir: Path) -> dict[str, Any]:
    detail_url = validate_url(str(job.get("detail_url") or ""))
    episode = int(job.get("episode", 1))
    verification_timeout = int(job.get("verification_timeout", 300))
    candidates: dict[str, dict[str, str]] = {}
    observations: list[dict[str, Any]] = []

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            viewport={"width": 1365, "height": 900},
            locale="en-US",
            args=["--autoplay-policy=no-user-gesture-required"],
        )
        page = context.pages[0] if context.pages else await context.new_page()

        async def capture(response: Response) -> None:
            try:
                url = response.url
                ct = response.headers.get("content-type", "")
                if media_priority(url, ct):
                    candidates[url] = {
                        "url": url,
                        "content_type": ct,
                        "status": str(response.status),
                        "source": "network",
                    }
                    return
                # Some players return the public media URL inside JSON/config text.
                if "json" in ct.lower() or "text" in ct.lower():
                    length = int(response.headers.get("content-length", "0") or 0)
                    if 0 < length > 2_000_000:
                        return
                    text = await response.text()
                    for found in re.findall(r"https?://[^\"'\\\s]+?(?:\.mp4|\.m3u8)(?:\?[^\"'\\\s]*)?", text, re.I):
                        clean = found.replace("\\/", "/")
                        candidates.setdefault(clean, {
                            "url": clean,
                            "content_type": "",
                            "status": "",
                            "source": "response-body",
                        })
            except Exception:
                pass

        page.on("response", capture)

        response = await page.goto(detail_url, wait_until="domcontentloaded", timeout=60000)
        observations.append({"event": "detail", "status": response.status if response else None, "url": page.url, "title": await page.title()})
        await wait_for_manual_verification(page, verification_timeout)
        await page.wait_for_timeout(2500)

        # Prefer an episode link exposed by the normal page.
        target_href: str | None = None
        try:
            links = await page.locator("a[href]").evaluate_all(
                "els => els.map(a => ({href:a.href,text:(a.innerText||a.textContent||'').trim()}))"
            )
            for link in links:
                href = str(link.get("href") or "")
                text = str(link.get("text") or "")
                parsed = urllib.parse.urlparse(href)
                qs = urllib.parse.parse_qs(parsed.query)
                ep_value = (qs.get("ep") or [""])[0]
                if ep_value == str(episode) or re.search(rf"\b(?:ep|episode)\s*{episode}\b", text, re.I):
                    target_href = href
                    break
        except Exception:
            pass

        if target_href:
            validate_url(target_href)
            response = await page.goto(target_href, wait_until="domcontentloaded", timeout=60000)
            observations.append({"event": "episode-link", "status": response.status if response else None, "url": page.url, "title": await page.title()})
            await wait_for_manual_verification(page, verification_timeout)
        else:
            # Public URL pattern used by the site's normal watch pages.
            parsed = urllib.parse.urlparse(detail_url)
            qs = urllib.parse.parse_qs(parsed.query)
            drama_id = (qs.get("id") or [""])[0]
            lang = (qs.get("lang") or ["en"])[0]
            if drama_id:
                watch_url = f"https://{ALLOWED_HOST}/index.php?ep={episode}&id={urllib.parse.quote(drama_id)}&lang={urllib.parse.quote(lang)}&view=watch"
                response = await page.goto(watch_url, wait_until="domcontentloaded", timeout=60000)
                observations.append({"event": "watch-fallback", "status": response.status if response else None, "url": page.url, "title": await page.title()})
                await wait_for_manual_verification(page, verification_timeout)

        await click_play_controls(page, episode)
        await page.wait_for_timeout(12000)

        try:
            sources = await page.locator("video,video source,source").evaluate_all(
                "els => els.map(e => e.currentSrc || e.src || e.getAttribute('src')).filter(Boolean)"
            )
            for src in sources:
                absolute = urllib.parse.urljoin(page.url, src)
                if absolute.startswith("http"):
                    candidates.setdefault(absolute, {"url": absolute, "content_type": "", "status": "", "source": "dom"})
        except Exception:
            pass

        observations.append({"event": "final", "url": page.url, "title": await page.title(), "candidate_count": len(candidates)})
        await page.screenshot(path=str(output_dir / "page.png"), full_page=True)
        cookies = await context.cookies()
        cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        user_agent = await page.evaluate("navigator.userAgent")
        referer = page.url
        await context.close()

    ordered = sorted(candidates.values(), key=lambda x: media_priority(x["url"], x.get("content_type", "")), reverse=True)
    headers = {"User-Agent": user_agent, "Referer": referer, "Accept": "*/*"}
    if cookie_header:
        headers["Cookie"] = cookie_header

    output_dir.mkdir(parents=True, exist_ok=True)
    errors: list[dict[str, str]] = []
    downloaded: dict[str, Any] | None = None
    for candidate in ordered:
        url = candidate["url"]
        ct = candidate.get("content_type", "")
        if not media_priority(url, ct):
            continue
        out = output_dir / f"Episode {episode:03d}.mp4"
        try:
            if ".m3u8" in url.lower() or "mpegurl" in ct.lower():
                download_hls(url, out, headers)
                media_type = "hls-unencrypted"
            else:
                download_direct(url, out, headers)
                media_type = "direct-video"
            if out.exists() and out.stat().st_size > 0:
                downloaded = {"path": str(out), "bytes": out.stat().st_size, "media_type": media_type, "source_url": url}
                break
        except Exception as exc:
            errors.append({"url": url, "error": f"{type(exc).__name__}: {exc}"})

    result = {
        "title": job.get("title"),
        "episode": episode,
        "detail_url": detail_url,
        "observations": observations,
        "candidates": ordered,
        "download": downloaded,
        "errors": errors,
    }
    (output_dir / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def upload_result(storage: DriveStorage, job: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    download = result.get("download")
    if not download:
        raise RuntimeError("No public unencrypted video was resolved for this episode.")

    title = safe_name(str(job.get("title") or "Untitled Drama"))
    episode = int(job.get("episode", 1))
    library = storage.ensure_folder("Library")
    platform_folder = storage.ensure_folder("DramaFren", library["id"])
    title_folder = storage.ensure_folder(title, platform_folder["id"])
    manifests = storage.ensure_folder("Manifests")

    media_path = Path(download["path"])
    drive_video = storage.upload_file(
        media_path,
        parent_id=title_folder["id"],
        name=f"Episode {episode:03d}.mp4",
        mime_type=mimetypes.guess_type(media_path.name)[0] or "video/mp4",
        properties={
            "kind": "video",
            "platform": "dramafren",
            "episode": str(episode),
            "title": title[:120],
        },
    )
    manifest = storage.upload_json(
        result,
        parent_id=manifests["id"],
        name=f"{safe_name(title)}-ep{episode:03d}.json",
        properties={"kind": "manifest", "platform": "dramafren"},
    )
    return {"drive_video": drive_video, "manifest": manifest}


def process_queue_once(storage: DriveStorage, profile_dir: Path) -> int:
    queue = storage.ensure_folder("Queue")
    done = storage.ensure_folder("Done")
    failed = storage.ensure_folder("Failed")
    jobs = storage.list_children(queue["id"], name_suffix=".json", limit=100)
    processed = 0

    for drive_job in jobs:
        processed += 1
        job_id = drive_job["id"]
        name = drive_job.get("name") or f"{job_id}.json"
        destination = done["id"]
        try:
            job = json.loads(storage.download_text(job_id))
            if str(job.get("platform") or "").lower() != "dramafren":
                raise ValueError("This queue accepts platform=dramafren jobs only.")
            if str(job.get("kind") or "episode").lower() != "episode":
                raise ValueError("The first worker version accepts kind=episode jobs only.")
            with tempfile.TemporaryDirectory(prefix="dramafren-") as td:
                result = asyncio.run(resolve_episode(job, Path(td), profile_dir))
                uploaded = upload_result(storage, job, result)
                final = {"ok": True, "job": job, "result": result, "uploaded": uploaded}
        except Exception as exc:
            destination = failed["id"]
            final = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        storage.upload_json(
            final,
            parent_id=destination,
            name=f"{Path(name).stem}.result.json",
            properties={"kind": "job_result", "platform": "dramafren"},
        )
        storage.move_file(job_id, from_parent=queue["id"], to_parent=destination)
        print(json.dumps({"job": name, **final}, ensure_ascii=False))
    return processed


def main() -> int:
    parser = argparse.ArgumentParser(description="Process public DramaFren episode jobs from Google Drive.")
    parser.add_argument("--root-folder-id", default=os.getenv("SHORT_DRAMA_DRIVE_ROOT_FOLDER_ID"))
    parser.add_argument("--profile-dir", default=os.getenv("DRAMAFREN_BROWSER_PROFILE", str(Path.home() / ".tokisclone" / "dramafren-browser")))
    parser.add_argument("--once", action="store_true", help="Process current queue once and exit.")
    parser.add_argument("--poll", type=int, default=15)
    args = parser.parse_args()
    if not args.root_folder_id:
        raise SystemExit("Set SHORT_DRAMA_DRIVE_ROOT_FOLDER_ID or pass --root-folder-id.")

    profile_dir = Path(args.profile_dir).expanduser()
    profile_dir.mkdir(parents=True, exist_ok=True)
    storage = DriveStorage(root_folder_id=args.root_folder_id)
    print("DramaFren Drive worker ready.")
    print("A Chrome window may open for manual Cloudflare verification. The worker does not bypass it.")

    if args.once:
        process_queue_once(storage, profile_dir)
        return 0

    while True:
        try:
            process_queue_once(storage, profile_dir)
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            print(f"Queue error: {type(exc).__name__}: {exc}")
        time.sleep(max(5, args.poll))


if __name__ == "__main__":
    raise SystemExit(main())
