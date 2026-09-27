"""Unhandled-exception reporting that reaches the screen, not just the logs.

A 500 from a backend service used to surface in the web UI as a bare "internal
server error", leaving the actual traceback in ``docker compose logs`` for
someone to go digging for. Every service installs this handler so the failure
travels with the response: the UI can show what broke without the user
shelling into the host.

This is a single-user application on a trusted LAN, behind a password, so the
traceback is not treated as sensitive. It is logged in full as well.
"""

from __future__ import annotations

import logging
import traceback
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def install_error_handler(app: FastAPI, service: str) -> None:
    """Middleware, not an ``Exception`` handler: Starlette re-raises after
    calling one of those so the ASGI server can log it, which means the caller
    never sees the response. Catching here returns it cleanly."""

    @app.middleware("http")
    async def unhandled(request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:  # noqa: BLE001 - this is the catch-all by design
            return _report(request, exc, service)


def _report(request: Request, exc: Exception, service: str) -> JSONResponse:
    error_id = uuid.uuid4().hex[:8]
    trace = traceback.format_exception(type(exc), exc, exc.__traceback__)
    logger.error(
        "[%s] unhandled error on %s %s\n%s",
        error_id,
        request.method,
        request.url.path,
        "".join(trace),
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": {
                "message": f"{service} hit an unexpected error handling "
                f"{request.method} {request.url.path}.",
                "error_id": error_id,
                "exception": f"{type(exc).__name__}: {exc}",
                "traceback": "".join(trace),
            }
        },
    )
