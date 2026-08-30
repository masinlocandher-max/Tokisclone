from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from media_core import (
    MANDATORY_SOURCE_POLICY,
    bulk_download,
    discover_profile,
    download_one,
    inspect_video_formats,
)

COOKIE_FILE = os.getenv("TOKISCLONE_COOKIE_FILE")


def diagnostic(job: dict[str, Any], out: Path) -> dict[str, Any]:
    text = str(job.get("message") or "Tokisclone bridge OK")
    path = out / "diagnostic.txt"
    path.write_text(text + "\n", encoding="utf-8")
    return {
        "kind": "diagnostic",
        "ok": True,
        "file": path.name,
        "source_policy": MANDATORY_SOURCE_POLICY,
    }


def inspect_url(job: dict[str, Any], out: Path) -> dict[str, Any]:
    url = str(job.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("inspect job requires an http(s) url")

    details = inspect_video_formats(url, cookie_file=COOKIE_FILE)
    path = out / "formats.json"
    path.write_text(
        json.dumps(details, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "kind": "inspect",
        "ok": True,
        **details,
        "file": path.name,
    }


def download_video(job: dict[str, Any], out: Path) -> dict[str, Any]:
    url = str(job.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("video job requires an http(s) url")

    result = download_one(
        url,
        out,
        quality=str(job.get("quality") or "best"),
        write_subtitles=bool(job.get("write_subtitles", False)),
        cookie_file=COOKIE_FILE,
    )
    return {"kind": "video", "ok": True, **result}


def profile_all(job: dict[str, Any], out: Path) -> dict[str, Any]:
    profile_url = str(job.get("profile_url") or "").strip()
    if not profile_url.startswith(("http://", "https://")):
        raise ValueError("profile job requires profile_url")

    seed_video_url = str(job.get("seed_video_url") or "").strip() or None

    inventory = discover_profile(
        profile_url,
        seed_video_url=seed_video_url,
        cookie_file=COOKIE_FILE,
    )
    inventory_path = out / "inventory.json"
    inventory_path.write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    summary = bulk_download(
        [item["url"] for item in inventory["videos"] if item.get("url")],
        out / "downloads",
        quality=str(job.get("quality") or "best"),
        write_subtitles=bool(job.get("write_subtitles", False)),
        cookie_file=COOKIE_FILE,
        retry_failed_once=bool(job.get("retry_failed_once", True)),
    )

    return {
        "kind": "profile",
        "ok": True,
        "complete": summary["failed"] == 0,
        "status": "completed" if summary["failed"] == 0 else "completed_with_errors",
        "profile_url": profile_url,
        "scope": "all_public",
        "discovered": inventory["count"],
        "discovery_method": inventory["discovery_method"],
        "inventory_file": inventory_path.name,
        **summary,
    }


def bulk_urls(job: dict[str, Any], out: Path) -> dict[str, Any]:
    raw_urls = job.get("urls")
    if not isinstance(raw_urls, list):
        raise ValueError("bulk_urls job requires a urls array")

    summary = bulk_download(
        [str(url) for url in raw_urls],
        out / "downloads",
        quality=str(job.get("quality") or "best"),
        write_subtitles=bool(job.get("write_subtitles", False)),
        cookie_file=COOKIE_FILE,
        retry_failed_once=bool(job.get("retry_failed_once", True)),
    )

    return {
        "kind": "bulk_urls",
        "ok": True,
        "complete": summary["failed"] == 0,
        "status": "completed" if summary["failed"] == 0 else "completed_with_errors",
        **summary,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("job")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    job_path = Path(args.job)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    job = json.loads(job_path.read_text(encoding="utf-8"))
    kind = str(job.get("kind") or "video").lower()

    try:
        if kind == "diagnostic":
            result = diagnostic(job, out)
        elif kind == "inspect":
            result = inspect_url(job, out)
        elif kind == "video":
            result = download_video(job, out)
        elif kind in {"profile", "bulk_profile"}:
            result = profile_all(job, out)
        elif kind == "bulk_urls":
            result = bulk_urls(job, out)
        else:
            raise ValueError(
                "Unsupported job kind. Use diagnostic, inspect, video, profile, "
                "bulk_profile, or bulk_urls."
            )
        status = 0
    except Exception as exc:
        result = {
            "kind": kind,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "source_policy": MANDATORY_SOURCE_POLICY,
        }
        status = 1

    (out / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False))
    return status


if __name__ == "__main__":
    sys.exit(main())
