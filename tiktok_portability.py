from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any, Iterator

TIKTOK_API_BASE = "https://open.tiktokapis.com"
PORTABILITY_SCOPE = "portability.postsandprofile.single"
MAX_EXPORT_BYTES = int(os.getenv("TIKTOK_PORTABILITY_MAX_EXPORT_BYTES", str(256 * 1024 * 1024)))
MAX_VIDEO_BYTES = int(os.getenv("TIKTOK_AUTHORIZED_MAX_VIDEO_BYTES", str(2 * 1024 * 1024 * 1024)))
USER_AGENT = "Tokisclone/1.0 (+authorized TikTok portability download)"


class TikTokPortabilityError(RuntimeError):
    pass


def _bearer(access_token: str) -> str:
    token = str(access_token or "").strip()
    if not token:
        raise ValueError("TikTok access token is required")
    return token


def _json_request(
    path: str,
    access_token: str,
    payload: dict[str, Any],
    *,
    fields: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    url = f"{TIKTOK_API_BASE}{path}"
    if fields:
        url += "?" + urllib.parse.urlencode({"fields": fields})

    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {_bearer(access_token)}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        detail = raw.decode("utf-8", "replace")[:4000]
        raise TikTokPortabilityError(
            f"TikTok API returned HTTP {exc.code}: {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise TikTokPortabilityError(f"TikTok API request failed: {exc.reason}") from exc

    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise TikTokPortabilityError("TikTok API returned non-JSON data") from exc

    if not isinstance(data, dict):
        raise TikTokPortabilityError("TikTok API returned an unexpected response")

    error = data.get("error")
    if isinstance(error, dict) and error.get("code"):
        raise TikTokPortabilityError(
            f"TikTok API error {error.get('code')}: {error.get('message') or error}"
        )

    return data


def create_video_export_request(access_token: str) -> dict[str, Any]:
    """Create an official TikTok Data Portability export for the user's own posts."""
    return _json_request(
        "/v2/user/data/add/",
        access_token,
        {
            "data_format": "json",
            "category_selection_list": ["video"],
        },
        fields="request_id",
    )


def check_video_export_status(access_token: str, request_id: int) -> dict[str, Any]:
    rid = int(request_id)
    if rid <= 0:
        raise ValueError("request_id must be a positive integer")
    return _json_request(
        "/v2/user/data/check/",
        access_token,
        {"request_id": rid},
        fields="request_id,apply_time,collect_time,status",
    )


def download_video_export_zip(access_token: str, request_id: int, *, timeout: int = 120) -> bytes:
    """Download the official portability ZIP. The access token is never persisted."""
    rid = int(request_id)
    if rid <= 0:
        raise ValueError("request_id must be a positive integer")

    request = urllib.request.Request(
        f"{TIKTOK_API_BASE}/v2/user/data/download/",
        data=json.dumps({"request_id": rid}).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {_bearer(access_token)}",
            "Content-Type": "application/json",
            "Accept": "application/zip,application/octet-stream,application/json",
            "User-Agent": USER_AGENT,
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content_type = (response.headers.get("Content-Type") or "").lower()
            raw = response.read(MAX_EXPORT_BYTES + 1)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:4000]
        raise TikTokPortabilityError(
            f"TikTok export download returned HTTP {exc.code}: {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise TikTokPortabilityError(
            f"TikTok export download failed: {exc.reason}"
        ) from exc

    if len(raw) > MAX_EXPORT_BYTES:
        raise TikTokPortabilityError(
            f"TikTok export exceeded the configured {MAX_EXPORT_BYTES} byte limit"
        )

    if "json" in content_type or raw[:1] == b"{":
        try:
            error = json.loads(raw.decode("utf-8"))
        except Exception:
            error = raw.decode("utf-8", "replace")[:4000]
        raise TikTokPortabilityError(f"TikTok export was not ready: {error}")

    if not zipfile.is_zipfile(BytesIO(raw)):
        raise TikTokPortabilityError("TikTok export response was not a valid ZIP file")

    return raw


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _looks_like_download_field(key: Any) -> bool:
    normalized = _normalized_key(key)
    return normalized in {
        "postedvideodownloadlink",
        "videodownloadlink",
        "postdownloadlink",
    } or (
        "video" in normalized and "download" in normalized and "link" in normalized
    )


def _safe_context(mapping: dict[str, Any]) -> dict[str, Any]:
    context: dict[str, Any] = {}
    wanted = {
        "date",
        "title",
        "posttitle",
        "videolandingpagelink",
        "link",
        "id",
        "videoid",
    }
    for key, value in mapping.items():
        if _normalized_key(key) not in wanted:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            context[str(key)] = value
    return context


def _walk_for_download_links(
    node: Any,
    *,
    path: tuple[str, ...] = (),
) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        context = _safe_context(node)
        for key, value in node.items():
            current_path = path + (str(key),)
            if (
                _looks_like_download_field(key)
                and isinstance(value, str)
                and value.startswith(("https://", "http://"))
            ):
                yield {
                    "download_url": value,
                    "field": str(key),
                    "path": "/".join(current_path),
                    "context": context,
                }
            yield from _walk_for_download_links(value, path=current_path)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk_for_download_links(
                value,
                path=path + (str(index),),
            )


def extract_posted_video_links(export_zip: bytes) -> list[dict[str, Any]]:
    """Extract official Posted Video Download Link records from a portability ZIP."""
    records: list[dict[str, Any]] = []
    seen: set[str] = set()

    with zipfile.ZipFile(BytesIO(export_zip)) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            if info.file_size > MAX_EXPORT_BYTES:
                continue

            name = info.filename
            suffix = Path(name).suffix.lower()
            if suffix != ".json":
                continue

            try:
                payload = json.loads(archive.read(info).decode("utf-8-sig"))
            except Exception:
                continue

            for record in _walk_for_download_links(payload):
                url = record["download_url"]
                if url in seen:
                    continue
                seen.add(url)
                record["export_file"] = name
                record["index"] = len(records)
                records.append(record)

    return records


def _is_public_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_public_media_url(url: str) -> str:
    """Reject arbitrary local/private targets before proxying an authorized media URL."""
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    if parsed.scheme != "https":
        raise ValueError("authorized media URL must use https")
    if not parsed.hostname:
        raise ValueError("authorized media URL must include a hostname")
    if parsed.username or parsed.password:
        raise ValueError("authorized media URL must not contain credentials")

    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        }
    except socket.gaierror as exc:
        raise ValueError("authorized media hostname could not be resolved") from exc

    if not addresses or not all(_is_public_ip(address) for address in addresses):
        raise ValueError("authorized media URL resolved to a non-public network address")

    return urllib.parse.urlunsplit(parsed)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        safe_url = validate_public_media_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, safe_url)


def _probe_video(path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {
            "resolution_verified": False,
            "width": None,
            "height": None,
            "duration": None,
        }

    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height:format=duration",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        data = json.loads(completed.stdout)
        streams = data.get("streams") or []
        stream = streams[0] if streams else {}
        duration = (data.get("format") or {}).get("duration")
        width = stream.get("width")
        height = stream.get("height")
        return {
            "resolution_verified": bool(width and height),
            "width": width,
            "height": height,
            "duration": float(duration) if duration is not None else None,
        }
    except Exception:
        return {
            "resolution_verified": False,
            "width": None,
            "height": None,
            "duration": None,
        }


def download_authorized_video(
    media_url: str,
    output_dir: str | Path,
    *,
    timeout: int = 120,
) -> dict[str, Any]:
    """Download the exact official source bytes without transcoding or watermark manipulation."""
    safe_url = validate_public_media_url(media_url)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    opener = urllib.request.build_opener(_SafeRedirectHandler())
    request = urllib.request.Request(
        safe_url,
        headers={
            "Accept": "video/mp4,video/*;q=0.9,application/octet-stream;q=0.8,*/*;q=0.1",
            "User-Agent": USER_AGENT,
        },
    )

    fd, temp_name = tempfile.mkstemp(
        prefix="tiktok-authorized-",
        suffix=".mp4",
        dir=str(output_dir),
    )
    os.close(fd)
    path = Path(temp_name)

    try:
        with opener.open(request, timeout=timeout) as response, path.open("wb") as handle:
            content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip()
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_VIDEO_BYTES:
                raise TikTokPortabilityError(
                    f"Authorized video exceeds the configured {MAX_VIDEO_BYTES} byte limit"
                )

            total = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_VIDEO_BYTES:
                    raise TikTokPortabilityError(
                        f"Authorized video exceeds the configured {MAX_VIDEO_BYTES} byte limit"
                    )
                handle.write(chunk)

        if path.stat().st_size == 0:
            raise TikTokPortabilityError("Authorized video download returned an empty file")

        probe = _probe_video(path)
        return {
            "path": str(path),
            "bytes": path.stat().st_size,
            "content_type": content_type or "video/mp4",
            "source": "tiktok_data_portability",
            "source_bytes_preserved": True,
            "watermark_manipulation": False,
            **probe,
        }
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def choose_download_record(
    records: list[dict[str, Any]],
    *,
    index: int = 0,
) -> dict[str, Any]:
    if not records:
        raise TikTokPortabilityError(
            "TikTok export contained no Posted Video Download Link records"
        )
    if index < 0 or index >= len(records):
        raise ValueError(f"index must be between 0 and {len(records) - 1}")
    return records[index]
