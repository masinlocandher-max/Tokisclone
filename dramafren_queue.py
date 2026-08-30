from __future__ import annotations

import os
import urllib.parse
from pathlib import Path
from typing import Any

from dramafren_bulk import ALLOWED_HOST, run_bulk
from drive_storage import DriveStorage


def canonical_detail_url(value: str) -> str:
    raw = str(value or "").strip()
    parsed = urllib.parse.urlparse(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host != ALLOWED_HOST:
        raise ValueError(f"Only public https://{ALLOWED_HOST}/ URLs are accepted.")

    query = urllib.parse.parse_qs(parsed.query)
    drama_id = str((query.get("id") or [""])[0]).strip()
    lang = str((query.get("lang") or ["en"])[0]).strip() or "en"
    if not drama_id:
        raise ValueError("DramaFren URL must contain a public id= parameter.")

    return (
        f"https://{ALLOWED_HOST}/index.php?"
        + urllib.parse.urlencode(
            {
                "id": drama_id,
                "lang": lang,
                "view": "detail",
            }
        )
    )


def browser_profile_dir() -> Path:
    value = os.getenv(
        "DRAMAFREN_BROWSER_PROFILE",
        str(Path.home() / ".tokisclone" / "dramafren-browser"),
    )
    path = Path(value).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def process_dramafren_queue_job(
    storage: DriveStorage,
    job: dict[str, Any],
) -> dict[str, Any]:
    source_url = str(
        job.get("url")
        or job.get("detail_url")
        or job.get("profile_url")
        or ""
    ).strip()
    if not source_url:
        raise ValueError("dramafren job requires url")

    detail_url = canonical_detail_url(source_url)
    verification_timeout = max(
        30,
        min(int(job.get("verification_timeout", 300)), 1800),
    )
    retry_failed_once = bool(job.get("retry_failed_once", True))

    result = run_bulk(
        detail_url,
        storage,
        browser_profile_dir(),
        verification_timeout=verification_timeout,
        retry_failed_once=retry_failed_once,
    )

    return {
        "kind": "dramafren",
        "platform": "dramafren",
        "source_url": source_url,
        "detail_url": detail_url,
        "scope": "all_public_listed_episodes",
        "source_policy": (
            "public direct video or unencrypted HLS only; "
            "no DRM/encryption/CAPTCHA/paywall bypass"
        ),
        **result,
    }
