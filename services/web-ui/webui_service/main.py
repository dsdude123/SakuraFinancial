"""web-ui: the IE6-compatible face of SakuraFinancial.

Every page renders server-side (Jinja2 -> HTML 4.01), every interaction is a
form POST followed by a redirect, and no page requires JavaScript. The rules
and the deliberate Win98 look live in docs/ie6-style-guide.md and are
enforced by tests/test_ie6_lint.py.
"""

from __future__ import annotations

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
from .rendering import make_templates, render
from .secret import resolve_secret_key


def create_app(
    clients: Clients | None = None,
    secret_key: str | None = None,
    data_dir: str | None = None,
) -> FastAPI:
    app = FastAPI(title="SakuraFinancial web-ui", version="1.0", docs_url=None, redoc_url=None)
    app.state.clients = clients or Clients()
    app.state.templates = make_templates()
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
        return render(
            request,
            "error.html",
            {
                "service": exc.service,
                "status": exc.status,
                "detail": exc.detail,
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
    ):
        app.include_router(module.router)
    return app
