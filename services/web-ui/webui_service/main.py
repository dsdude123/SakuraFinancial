"""web-ui: the IE6-compatible face of SakuraFinancial.

Every page renders server-side (Jinja2 -> HTML 4.01), every interaction is a
form POST followed by a redirect, and no page requires JavaScript. The rules
and the deliberate Win98 look live in docs/ie6-style-guide.md and are
enforced by tests/test_ie6_lint.py.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .auth import NotAuthenticated, require_login
from .clients import Clients, ServiceError
from .rendering import make_templates, render


def create_app(clients: Clients | None = None, secret_key: str | None = None) -> FastAPI:
    app = FastAPI(title="SakuraFinancial web-ui", version="1.0", docs_url=None, redoc_url=None)
    app.state.clients = clients or Clients()
    app.state.templates = make_templates()

    app.add_middleware(
        SessionMiddleware,
        secret_key=secret_key or os.environ.get("SECRET_KEY", "sakura-dev-secret"),
        max_age=60 * 60 * 24 * 30,
        same_site="lax",
    )
    app.mount(
        "/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static"
    )

    @app.exception_handler(NotAuthenticated)
    async def not_authenticated(request: Request, exc: NotAuthenticated):
        return RedirectResponse("/login", status_code=303)

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
        bills,
        budget,
        charts,
        home,
        imports,
        monthly,
        reports,
        setup,
        settings_pages,
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
    ):
        app.include_router(module.router)
    return app
