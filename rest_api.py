from __future__ import annotations

import hmac
import os
from typing import Any

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from drive_storage import DriveStorage
from server import _inspect_video, _save_video_to_drive

VALID_MODELS = {"tiny", "base", "small", "medium", "large-v3"}


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


async def health(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "service": "Tokisclone Video Download API",
            "drive_storage": True,
            "api_key_configured": bool(_configured_api_key()),
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


routes = [
    Route("/health", health, methods=["GET"]),
    Route("/v1/inspect", inspect, methods=["POST"]),
    Route("/v1/download", download, methods=["POST"]),
]

app = Starlette(debug=False, routes=routes)
