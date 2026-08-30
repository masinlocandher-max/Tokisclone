from __future__ import annotations

import argparse
import html as html_lib
import json
import re
import sys
from pathlib import Path
from typing import Any

from curl_cffi import requests


HTML_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


def _extract_rehydration(page_html: str) -> dict[str, Any]:
    patterns = [
        r'<script[^>]*id=["\']__UNIVERSAL_DATA_FOR_REHYDRATION__["\'][^>]*>(.*?)</script>',
        r'<script[^>]*id=["\']SIGI_STATE["\'][^>]*>(.*?)</script>',
    ]
    for pattern in patterns:
        match = re.search(pattern, page_html, re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        raw = html_lib.unescape(match.group(1)).strip()
        if not raw:
            continue
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    raise RuntimeError("TikTok public profile HTML did not expose rehydration data")


def _user_info(data: dict[str, Any]) -> dict[str, Any]:
    scope = data.get("__DEFAULT_SCOPE__")
    if isinstance(scope, dict):
        detail = scope.get("webapp.user-detail")
        if isinstance(detail, dict):
            value = detail.get("userInfo")
            if isinstance(value, dict):
                return value

    module = data.get("UserModule")
    if isinstance(module, dict):
        users = module.get("users")
        stats = module.get("stats")
        if isinstance(users, dict) and users:
            user = next((v for v in users.values() if isinstance(v, dict)), {})
            user_stats: dict[str, Any] = {}
            user_id = str(user.get("id") or "")
            if isinstance(stats, dict) and user_id in stats and isinstance(stats[user_id], dict):
                user_stats = stats[user_id]
            return {"user": user, "stats": user_stats}

    raise RuntimeError("TikTok rehydration data did not contain userInfo")


def _normalize_item(item: dict[str, Any], fallback_username: str) -> dict[str, Any] | None:
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


def _links_from_html(page_html: str, username: str, limit: int) -> list[dict[str, Any]]:
    escaped = re.escape(username.lstrip("@"))
    ids: list[str] = []
    for pattern in (
        rf'https?://www\.tiktok\.com/@{escaped}/video/(\d+)',
        rf'/@{escaped}/video/(\d+)',
    ):
        ids.extend(re.findall(pattern, page_html, re.IGNORECASE))
    return _dedupe(
        [
            {
                "id": video_id,
                "url": f"https://www.tiktok.com/@{username.lstrip('@')}/video/{video_id}",
            }
            for video_id in ids
        ],
        limit,
    )


def extract_profile(profile_url: str, limit: int = 50) -> dict[str, Any]:
    if not profile_url.startswith(("https://www.tiktok.com/@", "http://www.tiktok.com/@")):
        raise ValueError("profile_url must be a TikTok public profile URL")
    limit = max(1, min(int(limit), 500))
    requested_username = profile_url.rstrip("/").split("/")[-1].lstrip("@")

    session = requests.Session(impersonate="chrome")
    diagnostics: list[str] = []

    response = session.get(profile_url, headers=HTML_HEADERS, timeout=30, allow_redirects=True)
    diagnostics.append(f"profile_http={response.status_code}")
    diagnostics.append(f"profile_html_bytes={len(response.content)}")
    if response.status_code != 200:
        raise RuntimeError(f"TikTok profile returned HTTP {response.status_code}")

    page_html = response.text
    data = _extract_rehydration(page_html)
    info = _user_info(data)
    user = info.get("user") if isinstance(info.get("user"), dict) else {}
    stats = info.get("stats") if isinstance(info.get("stats"), dict) else {}

    username = str(user.get("uniqueId") or user.get("unique_id") or requested_username)
    sec_uid = str(user.get("secUid") or user.get("sec_uid") or "")
    diagnostics.append(f"sec_uid_present={bool(sec_uid)}")

    profile = {
        "username": username,
        "nickname": user.get("nickname"),
        "verified": user.get("verified"),
        "video_count": stats.get("videoCount"),
        "follower_count": stats.get("followerCount"),
        "following_count": stats.get("followingCount"),
        "like_count": stats.get("heartCount") or stats.get("heart"),
        "sec_uid": sec_uid,
    }

    videos: list[dict[str, Any]] = []
    source = "profile_html"

    if sec_uid:
        cursor = 0
        while len(videos) < limit:
            count = min(35, limit - len(videos))
            params: dict[str, str] = {
                "aid": "1988",
                "app_name": "tiktok_web",
                "device_platform": "web_pc",
                "count": str(count),
                "cursor": str(cursor),
                "secUid": sec_uid,
                "coverFormat": "2",
                "post_item_list_request_type": "0",
            }
            ms_token = session.cookies.get("msToken")
            if ms_token:
                params["msToken"] = ms_token

            api = session.get(
                "https://www.tiktok.com/api/post/item_list/",
                params=params,
                headers={
                    "Accept": "application/json",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": profile_url,
                },
                timeout=30,
            )
            diagnostics.append(f"post_api_http={api.status_code}")
            if api.status_code != 200:
                break
            try:
                payload = api.json()
            except Exception:
                diagnostics.append(f"post_api_non_json_bytes={len(api.content)}")
                break
            if not isinstance(payload, dict):
                break
            diagnostics.append(
                f"post_api_status={payload.get('statusCode', payload.get('status_code'))}"
            )
            items = payload.get("itemList") or payload.get("item_list") or []
            if not isinstance(items, list) or not items:
                break
            for item in items:
                if isinstance(item, dict):
                    normalized = _normalize_item(item, username)
                    if normalized:
                        videos.append(normalized)
            source = "public_post_api"
            if not payload.get("hasMore", payload.get("has_more", False)):
                break
            next_cursor = payload.get("cursor")
            if next_cursor is None or str(next_cursor) == str(cursor):
                break
            cursor = int(next_cursor)

    videos = _dedupe(videos, limit)
    if not videos:
        videos = _links_from_html(page_html, username, limit)
        source = "profile_html_links"

    if not videos:
        raise RuntimeError(
            "TikTok profile was readable but no public video list was exposed to this guest session; "
            + ", ".join(diagnostics)
        )

    return {
        "ok": True,
        "profile_url": profile_url,
        "profile": profile,
        "count": len(videos),
        "source": source,
        "diagnostics": diagnostics,
        "videos": videos,
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
        result = extract_profile(args.profile_url, args.limit)
        status = 0
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        status = 1

    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if result.get("ok"):
        (out / "inventory.json").write_text(
            json.dumps(result.get("videos", []), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (out / "profile.json").write_text(
            json.dumps(result.get("profile", {}), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    print(json.dumps(result, ensure_ascii=False))
    return status


if __name__ == "__main__":
    sys.exit(main())
