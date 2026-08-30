from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright


def _normalize(item: dict[str, Any], fallback_username: str) -> dict[str, Any] | None:
    video_id = str(item.get("id") or item.get("itemId") or "")
    if not video_id:
        return None
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    username = str(author.get("uniqueId") or author.get("unique_id") or fallback_username).lstrip("@")
    stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
    video = item.get("video") if isinstance(item.get("video"), dict) else {}
    return {
        "id": video_id,
        "url": f"https://www.tiktok.com/@{username}/video/{video_id}",
        "title": item.get("desc") or item.get("title"),
        "duration": video.get("duration"),
        "timestamp": item.get("createTime"),
        "view_count": stats.get("playCount"),
        "like_count": stats.get("diggCount"),
        "comment_count": stats.get("commentCount"),
        "share_count": stats.get("shareCount"),
        "thumbnail": video.get("cover") or video.get("originCover"),
    }


def _dedupe(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        video_id = str(row.get("id") or "")
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)
        result.append(row)
        if len(result) >= limit:
            break
    return result


def extract(profile_url: str, limit: int, out: Path) -> dict[str, Any]:
    limit = max(1, min(int(limit), 200))
    username = profile_url.rstrip("/").split("/")[-1].lstrip("@")
    captured: list[dict[str, Any]] = []
    response_notes: list[dict[str, Any]] = []

    with sync_playwright() as p:
        chrome_candidates = [
            os.getenv("CHROME_BIN"),
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
        ]
        executable = next((x for x in chrome_candidates if x and Path(x).exists()), None)
        launch_args: dict[str, Any] = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
        if executable:
            launch_args["executable_path"] = executable
        browser = p.chromium.launch(**launch_args)
        context = browser.new_context(
            locale="en-US",
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()

        def on_response(response):
            if "/api/post/item_list/" not in response.url:
                return
            note: dict[str, Any] = {"url": response.url[:500], "status": response.status}
            try:
                body = response.body()
                note["bytes"] = len(body)
                if body:
                    payload = json.loads(body.decode("utf-8", errors="replace"))
                    note["status_code"] = payload.get("statusCode", payload.get("status_code")) if isinstance(payload, dict) else None
                    items = payload.get("itemList") or payload.get("item_list") or [] if isinstance(payload, dict) else []
                    if isinstance(items, list):
                        for item in items:
                            if isinstance(item, dict):
                                normalized = _normalize(item, username)
                                if normalized:
                                    captured.append(normalized)
            except Exception as exc:
                note["error"] = f"{type(exc).__name__}: {exc}"
            response_notes.append(note)

        page.on("response", on_response)
        nav = page.goto(profile_url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(6000)

        for _ in range(4):
            page.mouse.wheel(0, 1600)
            page.wait_for_timeout(1500)
            if len(captured) >= limit:
                break

        hrefs = page.locator('a[href*="/video/"]').evaluate_all(
            "els => els.map(e => e.href).filter(Boolean)"
        )
        dom_rows: list[dict[str, Any]] = []
        for href in hrefs:
            match = re.search(r"/@([^/]+)/video/(\d+)", href)
            if match:
                dom_rows.append({"id": match.group(2), "url": href.split("?")[0]})

        title = page.title()
        current_url = page.url
        text = page.locator("body").inner_text(timeout=10000)[:5000]
        html = page.content()
        screenshot = out / "page.png"
        page.screenshot(path=str(screenshot), full_page=False)
        browser.close()

    videos = _dedupe(captured + dom_rows, limit)
    lower = (title + "\n" + text + "\n" + html[:20000]).lower()
    challenge_terms = [term for term in ("captcha", "verify to continue", "security verification", "unusual traffic") if term in lower]

    return {
        "ok": bool(videos),
        "profile_url": profile_url,
        "navigation_status": nav.status if nav else None,
        "current_url": current_url,
        "title": title,
        "body_excerpt": text,
        "challenge_terms": challenge_terms,
        "captured_api_responses": response_notes,
        "api_video_count": len(_dedupe(captured, limit)),
        "dom_video_count": len(_dedupe(dom_rows, limit)),
        "count": len(videos),
        "videos": videos,
        "screenshot": screenshot.name,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("profile_url")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    try:
        result = extract(args.profile_url, args.limit, out)
        status = 0 if result.get("ok") else 2
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        status = 1

    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if result.get("videos"):
        (out / "inventory.json").write_text(json.dumps(result["videos"], ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return status


if __name__ == "__main__":
    sys.exit(main())
