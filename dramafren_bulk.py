#!/usr/bin/env python3
"""One DramaFren detail URL -> every publicly listed episode -> Google Drive.

This is a bulk orchestrator over dramafren_drive_worker.resolve_episode(). It
uses the site's normal public episode list, a persistent user-controlled browser
session, and the existing strict access boundary: no CAPTCHA bypass, no DRM,
no encrypted HLS, and no paywall/authentication circumvention.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

from dramafren_drive_worker import (
    ALLOWED_HOST,
    resolve_episode,
    safe_name,
    upload_result,
    validate_url,
    wait_for_manual_verification,
)
from drive_storage import DriveStorage


def _episode_number(href: str, text: str = "") -> int | None:
    try:
        parsed = urllib.parse.urlparse(href)
        query = urllib.parse.parse_qs(parsed.query)
        raw = (query.get("ep") or [""])[0]
        if raw.isdigit() and int(raw) > 0:
            return int(raw)
    except Exception:
        pass
    match = re.search(r"\b(?:ep|episode)\s*(\d+)\b", text, re.I)
    return int(match.group(1)) if match else None


async def discover_drama(
    detail_url: str,
    profile_dir: Path,
    verification_timeout: int = 300,
) -> dict[str, Any]:
    """Read the public DramaFren detail page and return its full episode list."""
    detail_url = validate_url(detail_url)
    parsed_input = urllib.parse.urlparse(detail_url)
    input_query = urllib.parse.parse_qs(parsed_input.query)
    drama_id = (input_query.get("id") or [""])[0]
    language = (input_query.get("lang") or ["en"])[0]
    if not drama_id:
        raise ValueError("DramaFren URL must contain a public drama id= parameter.")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            viewport={"width": 1365, "height": 900},
            locale="en-US",
            args=["--autoplay-policy=no-user-gesture-required"],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        response = await page.goto(detail_url, wait_until="domcontentloaded", timeout=60000)
        await wait_for_manual_verification(page, verification_timeout)
        await page.wait_for_timeout(1800)

        title = ""
        try:
            heading = page.locator("h1").first
            if await heading.count():
                title = (await heading.inner_text()).strip()
        except Exception:
            pass
        if not title:
            title = (await page.title()).strip() or f"Drama {drama_id}"

        links = await page.locator("a[href]").evaluate_all(
            "els => els.map(a => ({href:a.href,text:(a.innerText||a.textContent||'').trim()}))"
        )
        body_text = ""
        try:
            body_text = await page.locator("body").inner_text(timeout=3000)
        except Exception:
            pass
        await context.close()

    episodes: dict[int, str] = {}
    for item in links:
        href = str(item.get("href") or "")
        text = str(item.get("text") or "")
        if not href:
            continue
        parsed = urllib.parse.urlparse(href)
        if (parsed.hostname or "").lower() != ALLOWED_HOST:
            continue
        query = urllib.parse.parse_qs(parsed.query)
        if (query.get("id") or [""])[0] != drama_id:
            continue
        number = _episode_number(href, text)
        if number:
            episodes[number] = href

    # Detail pages visibly publish a Total: N Eps count. If individual hrefs
    # are incomplete, generate only the site's documented public watch URL
    # pattern for the missing episode numbers.
    total_match = re.search(r"\bTotal\s*:\s*(\d+)\s*Eps?\b", body_text, re.I)
    total = int(total_match.group(1)) if total_match else (max(episodes) if episodes else 0)
    if total <= 0:
        raise RuntimeError("No public episode list was found on the DramaFren detail page.")

    for number in range(1, total + 1):
        episodes.setdefault(
            number,
            f"https://{ALLOWED_HOST}/index.php?ep={number}&id={urllib.parse.quote(drama_id)}&lang={urllib.parse.quote(language)}&view=watch",
        )

    return {
        "platform": "dramafren",
        "detail_url": detail_url,
        "drama_id": drama_id,
        "language": language,
        "title": title,
        "total_episodes": total,
        "scope": "all_public_listed_episodes",
        "episodes": [
            {"episode": n, "watch_url": episodes[n]}
            for n in sorted(episodes)
            if n <= total
        ],
        "http_status": response.status if response else None,
    }


def _already_on_drive(storage: DriveStorage, title: str, episode: int) -> bool:
    """Best-effort duplicate check against the title folder."""
    try:
        library = storage.ensure_folder("Library")
        platform = storage.ensure_folder("DramaFren", library["id"])
        title_folder = storage.ensure_folder(safe_name(title), platform["id"])
        expected = f"Episode {episode:03d}.mp4"
        children = storage.list_children(title_folder["id"], limit=1000)
        return any((item.get("name") or "") == expected for item in children)
    except Exception:
        return False


def run_bulk(
    detail_url: str,
    storage: DriveStorage,
    profile_dir: Path,
    *,
    verification_timeout: int = 300,
    retry_failed_once: bool = True,
) -> dict[str, Any]:
    inventory = asyncio.run(
        discover_drama(detail_url, profile_dir, verification_timeout)
    )
    title = inventory["title"]
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for entry in inventory["episodes"]:
        episode = int(entry["episode"])
        if _already_on_drive(storage, title, episode):
            skipped.append({"episode": episode, "reason": "already_on_drive"})
            continue

        job = {
            "kind": "episode",
            "platform": "dramafren",
            "detail_url": detail_url,
            "episode": episode,
            "title": title,
            "verification_timeout": verification_timeout,
        }
        attempts = 2 if retry_failed_once else 1
        last_error = "unknown error"
        for attempt in range(1, attempts + 1):
            try:
                with tempfile.TemporaryDirectory(prefix=f"dramafren-ep{episode:03d}-") as td:
                    result = asyncio.run(resolve_episode(job, Path(td), profile_dir))
                    uploaded = upload_result(storage, job, result)
                    results.append({
                        "episode": episode,
                        "attempt": attempt,
                        "download": result.get("download"),
                        "uploaded": uploaded,
                    })
                last_error = ""
                break
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < attempts:
                    time.sleep(2)
        if last_error:
            failures.append({"episode": episode, "error": last_error})

    summary = {
        "ok": not failures,
        "platform": "dramafren",
        "title": title,
        "detail_url": detail_url,
        "scope": "all_public_listed_episodes",
        "total_public_listed": inventory["total_episodes"],
        "downloaded": len(results),
        "skipped_existing": len(skipped),
        "failed": len(failures),
        "inventory": inventory,
        "results": results,
        "skipped": skipped,
        "failures": failures,
        "access_boundary": "public direct video or unencrypted HLS only; no DRM/encryption/CAPTCHA/paywall bypass",
    }

    manifests = storage.ensure_folder("Manifests")
    storage.upload_json(
        summary,
        parent_id=manifests["id"],
        name=f"{safe_name(title)}-all-episodes.json",
        properties={"kind": "bulk_manifest", "platform": "dramafren"},
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download every public listed episode from one DramaFren drama URL to Google Drive."
    )
    parser.add_argument("detail_url")
    parser.add_argument("--root-folder-id", default=os.getenv("SHORT_DRAMA_DRIVE_ROOT_FOLDER_ID"))
    parser.add_argument(
        "--profile-dir",
        default=os.getenv("DRAMAFREN_BROWSER_PROFILE", str(Path.home() / ".tokisclone" / "dramafren-browser")),
    )
    parser.add_argument("--verification-timeout", type=int, default=300)
    parser.add_argument("--no-retry", action="store_true")
    args = parser.parse_args()

    if not args.root_folder_id:
        raise SystemExit("Set SHORT_DRAMA_DRIVE_ROOT_FOLDER_ID or pass --root-folder-id.")

    profile_dir = Path(args.profile_dir).expanduser()
    profile_dir.mkdir(parents=True, exist_ok=True)
    storage = DriveStorage(root_folder_id=args.root_folder_id)
    summary = run_bulk(
        args.detail_url,
        storage,
        profile_dir,
        verification_timeout=args.verification_timeout,
        retry_failed_once=not args.no_retry,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not summary["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
