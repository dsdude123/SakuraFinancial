"""Single-user auth for the web UI.

The password hash lives in the settings service (key ``ui.password_hash``) so
it's part of normal backups and can be reset from the DB if forgotten (see
docs/runbook.md). The session is a signed cookie via Starlette's
SessionMiddleware — no server-side session state, which suits a single-user
LAN app (and IE6 handles plain cookies fine).
"""

from __future__ import annotations

import hashlib
import secrets

from fastapi import Request

PASSWORD_KEY = "ui.password_hash"
ITERATIONS = 200_000


class NotAuthenticated(Exception):
    """Raised by the auth dependency; the app handler redirects to /login."""


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), ITERATIONS
    ).hex()
    return f"pbkdf2${ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _scheme, iterations, salt, digest = stored.split("$")
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations)
        ).hex()
        return secrets.compare_digest(candidate, digest)
    except (ValueError, TypeError):
        return False


async def get_stored_hash(request: Request) -> str | None:
    data = await request.app.state.clients.settings.get(f"/api/settings/{PASSWORD_KEY}", params={"reveal": "true"})
    return data.get("value") or None


def require_login(request: Request) -> None:
    """Dependency on every page router except login/first-run."""
    if not request.session.get("authenticated"):
        raise NotAuthenticated()
