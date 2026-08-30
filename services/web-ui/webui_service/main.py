"""web-ui: the IE6-compatible face of SakuraFinancial.

Every page renders server-side (Jinja2 -> HTML 4.01), every interaction is a
form POST followed by a redirect, and no page requires JavaScript. The rules
and the deliberate Win98 look live in docs/ie6-style-guide.md and are
enforced by tests/test_ie6_lint.py.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .auth import NotAuthenticated, require_login
from .clients import Clients, ServiceError
from .errorlog import ErrorLog
from .rendering import make_templates, render
from .secret import resolve_secret_key

logger = logging.getLogger(__name__)


def create_app(
    clients: Clients | None = None,
    secret_key: str | None = None,
    data_dir: str | None = None,
) -> FastAPI:
    app = FastAPI(title="SakuraFinancial web-ui", version="1.0", docs_url=None, redoc_url=None)
    app.state.clients = clients or Clients()
    app.state.templates = make_templates()
    app.state.errors = ErrorLog()
    # The writable volume: the session key lives here, and so does the safety
    # backup parked mid-reset.
    app.state.data_dir = data_dir or os.environ.get("WEBUI_DATA_DIR", "/data/webui")

    app.add_middleware(
        SessionMiddleware,
        # Never a shipped constant — see webui_service/secret.py.
        secret_key=secret_key or resolve_secret_key(),
        max_age=60 * 60 * 24 * 30,
        same_site="lax",
    )
    app.mount(
        "/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static"
    )

    @app.exception_handler(NotAuthenticated)
    async def not_authenticated(request: Request, exc: NotAuthenticated):
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(RequestValidationError)
    async def invalid_form(request: Request, exc: RequestValidationError):
        """A blank or malformed field must never dump FastAPI's JSON at the
        user — this is a Windows 98 desktop app as far as they're concerned.
        Send them back to the form they were filling in with a plain-English
        note about which field is wrong."""
        fields = []
        for error in exc.errors():
            name = next(
                (str(part) for part in reversed(error.get("loc", ())) if isinstance(part, str)),
                "",
            )
            if name and name not in ("body", "query", "path") and name not in fields:
                fields.append(name)
        listed = ", ".join(field.replace("_", " ") for field in fields)
        message = (
            f"Check these fields and try again: {listed}."
            if listed
            else "Some of the values in that form could not be read."
        )
        referer = request.headers.get("referer", "")
        if referer.startswith(str(request.base_url).rstrip("/")):
            return RedirectResponse(
                referer + ("&" if "?" in referer else "?") + "err=" + quote(message),
                status_code=303,
            )
        return render(request, "error.html", {"service": "web UI", "status": 422, "detail": message})

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError):
        detail = exc.detail
        traceback_text = ""
        exception_text = ""
        message = detail
        if isinstance(detail, dict):
            message = detail.get("message", detail)
            traceback_text = detail.get("traceback", "")
            exception_text = detail.get("exception", "")
        error_id = app.state.errors.record(
            service=exc.service,
            status=exc.status,
            message=str(message),
            traceback=traceback_text,
            exception=exception_text,
            path=request.url.path,
        )
        return render(
            request,
            "error.html",
            {
                "service": exc.service,
                "status": exc.status,
                "detail": detail,
                "error_id": error_id,
                "has_trace": bool(traceback_text),
            },
        )

    @app.middleware("http")
    async def unhandled(request: Request, call_next):
        """The web UI's own failures get the same treatment as a backend's.
        Middleware rather than an Exception handler: Starlette re-raises after
        those, so the rendered page would never reach the browser."""
        import traceback as tb

        try:
            return await call_next(request)
        except ServiceError:
            raise
        except Exception as exc:  # noqa: BLE001 - catch-all by design
            pass
        trace = "".join(tb.format_exception(type(exc), exc, exc.__traceback__))
        logger.error("web UI error on %s %s\n%s", request.method, request.url.path, trace)
        error_id = app.state.errors.record(
            service="web UI",
            status=500,
            message="The page could not be rendered.",
            traceback=trace,
            exception=f"{type(exc).__name__}: {exc}",
            path=request.url.path,
        )
        return render(
            request,
            "error.html",
            {
                "service": "web UI",
                "status": 500,
                "detail": "The page could not be rendered.",
                "error_id": error_id,
                "has_trace": True,
            },
        )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    from .routers import (
        accounts,
        auth_pages,
        backup,
        bills,
        budget,
        charts,
        errors,
        home,
        imports,
        monthly,
        receipts_pages,
        reports,
        reset,
        setup,
        settings_pages,
        stocks_pages,
    )

    app.include_router(auth_pages.router)
    for module in (
        home,
        accounts,
        bills,
        budget,
        imports,
        monthly,
        reports,
        charts,
        setup,
        settings_pages,
        stocks_pages,
        receipts_pages,
        backup,
        reset,
        errors,
    ):
        app.include_router(module.router)
    return app
