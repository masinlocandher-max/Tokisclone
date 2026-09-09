from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from drive_storage import DriveStorage
from server import _inspect_video, _save_video_to_drive
from tiktok_portability import (
    check_video_export_status,
    choose_download_record,
    create_video_export_request,
    download_authorized_video,
    download_video_export_zip,
    extract_posted_video_links,
)

VALID_MODELS = {"tiny", "base", "small", "medium", "large-v3"}
TIKTOK_AUTHORIZED_DIR = Path(
    os.getenv("VIDEO_DOWNLOAD_DIR", "/tmp/tokisclone")
) / "tiktok-authorized"
TIKTOK_AUTHORIZED_DIR.mkdir(parents=True, exist_ok=True)


def _configured_api_key() -> str:
    return os.getenv("TOKISCLONE_API_KEY", "").strip()


def _authorized(request: Request) -> bool:
    expected = _configured_api_key()
    supplied = request.headers.get("x-api-key", "").strip()
    return bool(expected and supplied and hmac.compare_digest(expected, supplied))


def _auth_error() -> JSONResponse:
    if not _configured_api_key():
        return JSONResponse(
            {
                "ok": False,
                "error": "TOKISCLONE_API_KEY is not configured on the server.",
            },
            status_code=503,
        )
    return JSONResponse({"ok": False, "error": "Unauthorized."}, status_code=401)


def _valid_http_url(value: Any) -> str:
    url = str(value or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("url must be an http(s) URL")
    return url


def _tiktok_access_token(request: Request) -> str:
    token = request.headers.get("x-tiktok-access-token", "").strip()
    if not token:
        raise ValueError(
            "X-TikTok-Access-Token header is required for authorized TikTok downloads"
        )
    return token


def _request_id(value: Any) -> int:
    try:
        request_id = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("request_id must be a positive integer") from exc
    if request_id <= 0:
        raise ValueError("request_id must be a positive integer")
    return request_id


def _safe_unlink(path: str) -> None:
    try:
        Path(path).unlink()
    except OSError:
        pass


async def health(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "service": "Tokisclone Video Download API",
            "drive_storage": True,
            "api_key_configured": bool(_configured_api_key()),
            "tiktok_authorized_download": {
                "enabled": True,
                "provider": "TikTok Data Portability API",
                "source_bytes_preserved": True,
                "watermark_manipulation": False,
                "quality": "source-provided; verified when ffprobe is available",
            },
        }
    )


async def inspect(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _auth_error()

    try:
        payload = await request.json()
        url = _valid_http_url(payload.get("url"))
        result = await run_in_threadpool(_inspect_video, url)
        return JSONResponse({"ok": True, "video": result})
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            status_code=400,
        )


async def download(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _auth_error()

    try:
        payload = await request.json()
        url = _valid_http_url(payload.get("url"))
        transcribe = bool(payload.get("transcribe", False))
        model_size = str(payload.get("model_size") or "small")
        language = str(payload.get("language") or "").strip() or None

        if model_size not in VALID_MODELS:
            raise ValueError(
                "model_size must be one of: " + ", ".join(sorted(VALID_MODELS))
            )

        storage = DriveStorage()
        result = await run_in_threadpool(
            _save_video_to_drive,
            storage,
            url,
            transcribe=transcribe,
            model_size=model_size,
            language=language,
        )
        return JSONResponse({"ok": True, "result": result})
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            status_code=400,
        )


async def tiktok_authorized_request(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _auth_error()

    try:
        access_token = _tiktok_access_token(request)
        result = await run_in_threadpool(create_video_export_request, access_token)
        return JSONResponse(
            {
                "ok": True,
                "provider": "tiktok_data_portability",
                "result": result,
            },
            headers={"Cache-Control": "no-store"},
        )
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            status_code=400,
        )


async def tiktok_authorized_status(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _auth_error()

    try:
        payload = await request.json()
        request_id = _request_id(payload.get("request_id"))
        access_token = _tiktok_access_token(request)
        result = await run_in_threadpool(
            check_video_export_status,
            access_token,
            request_id,
        )
        return JSONResponse(
            {
                "ok": True,
                "provider": "tiktok_data_portability",
                "result": result,
            },
            headers={"Cache-Control": "no-store"},
        )
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            status_code=400,
        )


async def tiktok_authorized_links(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _auth_error()

    try:
        payload = await request.json()
        request_id = _request_id(payload.get("request_id"))
        access_token = _tiktok_access_token(request)
        export_zip = await run_in_threadpool(
            download_video_export_zip,
            access_token,
            request_id,
        )
        records = await run_in_threadpool(extract_posted_video_links, export_zip)
        return JSONResponse(
            {
                "ok": True,
                "provider": "tiktok_data_portability",
                "request_id": request_id,
                "count": len(records),
                "videos": records,
                "quality_note": (
                    "These are official source links from the user's TikTok portability export. "
                    "Tokisclone does not upscale, transcode, or manipulate watermarks."
                ),
            },
            headers={"Cache-Control": "no-store"},
        )
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            status_code=400,
        )


async def tiktok_authorized_download(request: Request):
    if not _authorized(request):
        return _auth_error()

    downloaded_path: str | None = None
    try:
        payload = await request.json()
        request_id = _request_id(payload.get("request_id"))
        index = int(payload.get("index", 0))
        access_token = _tiktok_access_token(request)

        export_zip = await run_in_threadpool(
            download_video_export_zip,
            access_token,
            request_id,
        )
        records = await run_in_threadpool(extract_posted_video_links, export_zip)
        record = choose_download_record(records, index=index)

        result = await run_in_threadpool(
            download_authorized_video,
            record["download_url"],
            TIKTOK_AUTHORIZED_DIR,
        )
        downloaded_path = str(result["path"])

        content_type = str(result.get("content_type") or "video/mp4")
        if not content_type.startswith("video/"):
            content_type = "video/mp4"

        headers = {
            "X-Tokisclone-Source": "tiktok-data-portability",
            "X-Tokisclone-Source-Bytes-Preserved": "true",
            "X-Tokisclone-Watermark-Manipulation": "false",
            "X-Tokisclone-Resolution-Verified": (
                "true" if result.get("resolution_verified") else "false"
            ),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        }
        if result.get("width"):
            headers["X-Tokisclone-Width"] = str(result["width"])
        if result.get("height"):
            headers["X-Tokisclone-Height"] = str(result["height"])

        filename = f"tiktok-authorized-{request_id}-{index}.mp4"
        return FileResponse(
            downloaded_path,
            media_type=content_type,
            filename=filename,
            headers=headers,
            background=BackgroundTask(_safe_unlink, downloaded_path),
        )
    except Exception as exc:
        if downloaded_path:
            _safe_unlink(downloaded_path)
        return JSONResponse(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            status_code=400,
        )


routes = [
    Route("/health", health, methods=["GET"]),
    Route("/v1/inspect", inspect, methods=["POST"]),
    Route("/v1/download", download, methods=["POST"]),
    Route("/v1/tiktok/authorized/request", tiktok_authorized_request, methods=["POST"]),
    Route("/v1/tiktok/authorized/status", tiktok_authorized_status, methods=["POST"]),
    Route("/v1/tiktok/authorized/links", tiktok_authorized_links, methods=["POST"]),
    Route(
        "/v1/tiktok/authorized/download",
        tiktok_authorized_download,
        methods=["POST"],
    ),
]

app = Starlette(debug=False, routes=routes)
