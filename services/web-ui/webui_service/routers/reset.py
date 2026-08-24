"""Factory reset: erase everything, but never without a backup in hand.

Wiping two decades of records is the one irreversible thing this app can do,
so the flow refuses to let the user reach the wipe without first taking a
backup *and* actually saving it:

    1. /reset                  what will be erased, and the button to start
    2. POST /reset/backup      builds the safety backup, parks it on the
                               web-ui data volume, hands back a token
    3. GET  /reset/download    the browser saves the zip; only now is the
                               token marked downloaded
    4. POST /reset/confirm     requires the token, the downloaded mark, and
                               the word ERASE typed out; then wipes

Step 3 is the gate that matters. A backup the user never received is not a
backup, so the confirm step checks the download happened rather than trusting
that the button was clicked. The zip lives on disk, not in memory — receipt
scans make these archives large.

No JavaScript, per docs/ie6-style-guide.md: each step is a form post and a
redirect, and the confirmation is an ordinary intermediate page.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request

from ..auth import PASSWORD_KEY, require_login
from ..clients import ServiceError
from ..rendering import render
from .accounts import back
from .backup import SERVICES, backup_filename, build_backup_zip, zip_response

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_login)])

CONFIRM_WORD = "ERASE"

# Receipts first (it points at ledger transactions), ledger last of the data
# services, settings last of all so its password survives the longest.
RESET_ORDER = ("receipts", "budget", "stocks", "ledger", "settings")

# A parked backup older than this is stale — the user walked away mid-flow.
PENDING_MAX_AGE = dt.timedelta(hours=6)


def pending_dir(request: Request) -> Path:
    return Path(request.app.state.data_dir) / "pending-reset"


def _paths(directory: Path, token: str) -> tuple[Path, Path]:
    safe = Path(token).name  # tokens are ours, but never trust a path from a URL
    return directory / f"{safe}.zip", directory / f"{safe}.json"


def save_pending(directory: Path, payload: bytes) -> str:
    """Park a freshly built backup and return its token."""
    token = secrets.token_urlsafe(16)
    directory.mkdir(parents=True, exist_ok=True)
    zip_path, meta_path = _paths(directory, token)
    zip_path.write_bytes(payload)
    meta_path.write_text(
        json.dumps(
            {
                "created_at": dt.datetime.now().isoformat(timespec="seconds"),
                "filename": backup_filename(),
                "size": len(payload),
                "downloaded_at": None,
            }
        )
    )
    return token


def load_pending(directory: Path, token: str) -> dict | None:
    if not token:
        return None
    zip_path, meta_path = _paths(directory, token)
    if not zip_path.exists() or not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, ValueError):
        return None
    created = dt.datetime.fromisoformat(meta["created_at"])
    if dt.datetime.now() - created > PENDING_MAX_AGE:
        discard_pending(directory, token)
        return None
    return {**meta, "token": token}


def mark_downloaded(directory: Path, token: str) -> None:
    _, meta_path = _paths(directory, token)
    meta = json.loads(meta_path.read_text())
    meta["downloaded_at"] = dt.datetime.now().isoformat(timespec="seconds")
    meta_path.write_text(json.dumps(meta))


def discard_pending(directory: Path, token: str) -> None:
    for path in _paths(directory, token):
        try:
            path.unlink()
        except OSError:
            pass


def sweep_stale(directory: Path) -> None:
    """Drop abandoned backups so the volume doesn't collect copies of the
    entire database."""
    if not directory.is_dir():
        return
    cutoff = dt.datetime.now() - PENDING_MAX_AGE
    for meta_path in directory.glob("*.json"):
        try:
            created = dt.datetime.fromisoformat(json.loads(meta_path.read_text())["created_at"])
        except (OSError, ValueError, KeyError):
            created = dt.datetime.min
        if created < cutoff:
            discard_pending(directory, meta_path.stem)


def human_size(byte_count: int) -> str:
    size = float(byte_count)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


@router.get("/reset")
async def reset_page(request: Request, token: str = ""):
    directory = pending_dir(request)
    sweep_stale(directory)
    pending = load_pending(directory, token)
    if token and pending is None:
        return back("/reset", err="That safety backup expired - start again.")
    return render(
        request,
        "reset.html",
        {
            "pending": pending,
            "pending_size": human_size(pending["size"]) if pending else "",
            "confirm_word": CONFIRM_WORD,
            "services": SERVICES,
        },
    )


@router.post("/reset/backup")
async def reset_backup(request: Request):
    """Step 2: build the safety backup before anything can be erased."""
    try:
        payload = await build_backup_zip(request.app.state.clients)
    except ServiceError as exc:
        return back(
            "/reset",
            err=f"Could not back up {exc.service} ({exc.detail}). Nothing was erased - "
            "every service must be reachable before a reset can start.",
        )
    token = save_pending(pending_dir(request), payload)
    return back(f"/reset?token={token}")


@router.get("/reset/download")
async def reset_download(request: Request, token: str = ""):
    """Step 3: hand over the zip. Reaching the wipe requires passing here."""
    directory = pending_dir(request)
    pending = load_pending(directory, token)
    if pending is None:
        return back("/reset", err="That safety backup expired - start again.")
    zip_path, _ = _paths(directory, token)
    mark_downloaded(directory, token)
    return zip_response(zip_path.read_bytes(), pending["filename"])


@router.post("/reset/cancel")
async def reset_cancel(request: Request, token: str = Form("")):
    discard_pending(pending_dir(request), token)
    return back("/settings", msg="Reset cancelled - nothing was erased.")


@router.post("/reset/confirm")
async def reset_confirm(
    request: Request, token: str = Form(""), confirm: str = Form(""), understood: str = Form("")
):
    """Step 4: the wipe. Every gate is re-checked here, not just in the UI."""
    directory = pending_dir(request)
    pending = load_pending(directory, token)
    if pending is None:
        return back("/reset", err="That safety backup expired - start again.")
    if not pending.get("downloaded_at"):
        return back(
            f"/reset?token={token}",
            err="Download the backup first - the reset stays locked until the zip "
            "has actually been sent to your browser.",
        )
    if understood != "on":
        return back(f"/reset?token={token}", err="Tick the box to confirm you understand.")
    if confirm.strip().upper() != CONFIRM_WORD:
        return back(f"/reset?token={token}", err=f"Type {CONFIRM_WORD} to confirm.")

    clients = request.app.state.clients
    wiped: list[str] = []
    for name in RESET_ORDER:
        # The login password is deliberately spared: a factory reset clears
        # your data, it doesn't lock you out of the machine.
        body = {"keep": [PASSWORD_KEY]} if name == "settings" else {}
        try:
            await getattr(clients, name).post("/api/reset", json=body)
        except ServiceError as exc:
            logger.error("reset stopped at %s: %s", name, exc.detail)
            return back(
                "/backup",
                err=f"Reset stopped at {name}: {exc.detail}. Erased so far: "
                f"{', '.join(wiped) or 'nothing'}. Your backup zip is already saved - "
                "restore it from this page to get back to where you were.",
            )
        wiped.append(name)
    discard_pending(directory, token)
    logger.warning("factory reset completed: %s", ", ".join(wiped))
    return back(
        "/",
        msg=f"Everything erased ({', '.join(wiped)}). Your password still works; "
        "keep that backup zip somewhere safe.",
    )
