from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from curl_cffi import requests

from tiktok_guest import HTML_HEADERS, _dedupe, _extract_rehydration, _normalize_item, _user_info


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def extract_embedded(profile_url: str, limit: int = 50) -> dict[str, Any]:
    limit = max(1, min(int(limit), 500))
    username = profile_url.rstrip("/").split("/")[-1].lstrip("@")
    session = requests.Session(impersonate="chrome")
    response = session.get(profile_url, headers=HTML_HEADERS, timeout=30, allow_redirects=True)
    if response.status_code != 200:
        raise RuntimeError(f"TikTok profile returned HTTP {response.status_code}")

    data = _extract_rehydration(response.text)
    user_info = _user_info(data)
    user = user_info.get("user") if isinstance(user_info.get("user"), dict) else {}
    resolved_username = str(user.get("uniqueId") or user.get("unique_id") or username)

    videos: list[dict[str, Any]] = []
    candidate_shapes: dict[str, int] = {}
    numeric_ids: list[str] = []

    for node in _walk(data):
        keys = tuple(sorted(str(k) for k in node.keys()))
        if keys:
            shape = ",".join(keys[:12])
            candidate_shapes[shape] = candidate_shapes.get(shape, 0) + 1

        node_id = str(node.get("id") or node.get("itemId") or "")
        if re.fullmatch(r"\d{15,22}", node_id):
            numeric_ids.append(node_id)

        looks_like_video = (
            isinstance(node.get("video"), dict)
            or (
                isinstance(node.get("stats"), dict)
                and any(k in node.get("stats", {}) for k in ("playCount", "diggCount", "commentCount"))
            )
            or ("createTime" in node and ("desc" in node or "author" in node))
        )
        if not looks_like_video:
            continue
        normalized = _normalize_item(node, resolved_username)
        if normalized:
            videos.append(normalized)

    videos = _dedupe(videos, limit)
    return {
        "ok": bool(videos),
        "profile_url": profile_url,
        "username": resolved_username,
        "count": len(videos),
        "videos": videos,
        "numeric_ids_sample": list(dict.fromkeys(numeric_ids))[:30],
        "top_shapes": sorted(candidate_shapes.items(), key=lambda kv: kv[1], reverse=True)[:30],
        "rehydration_top_keys": list(data.keys())[:50],
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
        result = extract_embedded(args.profile_url, args.limit)
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
