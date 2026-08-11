"""Session-signing key resolution.

Everything else in this stack is turn-key with no configuration, but the key
that signs login cookies must NOT be a shipped constant: anyone who read this
repository could forge a session cookie and walk past the password. So the
key is generated randomly on first boot and persisted to the web-ui data
volume, which keeps setup at zero while staying unique per installation.

Precedence:
    1. ``SECRET_KEY`` in the environment (for people who manage their own).
    2. The persisted key file on the data volume (created on first boot).
    3. A random ephemeral key, with a warning — sessions then end whenever
       the container restarts, which is annoying but never insecure.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

logger = logging.getLogger(__name__)

KEY_FILENAME = "session_secret"


def resolve_secret_key(data_dir: str | None = None) -> str:
    from_env = os.environ.get("SECRET_KEY", "").strip()
    if from_env:
        return from_env

    directory = Path(data_dir or os.environ.get("WEBUI_DATA_DIR", "/data/webui"))
    key_path = directory / KEY_FILENAME
    try:
        if key_path.exists():
            existing = key_path.read_text().strip()
            if existing:
                return existing
        directory.mkdir(parents=True, exist_ok=True)
        generated = secrets.token_urlsafe(48)
        key_path.write_text(generated)
        key_path.chmod(0o600)
        logger.info("generated a new session signing key at %s", key_path)
        return generated
    except OSError as exc:
        logger.warning(
            "could not persist a session key in %s (%s) — using a temporary key; "
            "logins will not survive a restart",
            directory,
            exc,
        )
        return secrets.token_urlsafe(48)
