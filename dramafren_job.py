from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dramafren_adapter import discover_drama, download_drama

COOKIE_FILE = os.getenv("TOKISCLONE_COOKIE_FILE")


def run_dramafren_job(job: dict[str, Any], out: str | Path) -> dict[str, Any]:
    url = str(job.get("url") or job.get("profile_url") or "").strip()
    if not url:
        raise ValueError("dramafren job requires url")

    output = Path(out)
    output.mkdir(parents=True, exist_ok=True)

    if bool(job.get("inventory_only", False)):
        inventory = discover_drama(url, cookie_file=COOKIE_FILE)
        path = output / "inventory.json"
        path.write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "kind": "dramafren",
            "ok": True,
            "inventory_only": True,
            "drama_id": inventory["drama_id"],
            "title": inventory.get("title"),
            "episodes": inventory["count"],
            "inventory_file": path.name,
        }

    result = download_drama(
        url,
        output,
        cookie_file=COOKIE_FILE,
        retry_failed_once=bool(job.get("retry_failed_once", True)),
    )
    return {
        "kind": "dramafren",
        "ok": True,
        "complete": result["failed"] == 0,
        "status": "completed" if result["failed"] == 0 else "completed_with_errors",
        **result,
    }
