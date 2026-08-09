"""One-click disaster recovery.

Download: a zip with every service's human-readable JSON export plus the
original receipt files. Restore: upload that zip into a fresh stack and get
everything back — the whole reason this platform exists is that the old
Windows 98 machine could die and take two decades of records with it.
"""

from __future__ import annotations

import datetime as dt
import json
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response

from ..auth import require_login
from ..clients import ServiceError
from ..rendering import render
from .accounts import back

router = APIRouter(dependencies=[Depends(require_login)])

# Restore order matters: settings first (provider config), then ledger
# (everything references it), then the derived/linked services.
SERVICES = ("settings", "ledger", "budget", "stocks", "receipts")


@router.get("/backup")
async def backup_page(request: Request):
    clients = request.app.state.clients
    reachable = {}
    for name in SERVICES:
        try:
            await getattr(clients, name).get("/health")
            reachable[name] = True
        except ServiceError:
            reachable[name] = False
    return render(request, "backup.html", {"reachable": reachable})


@router.get("/backup/download")
async def backup_download(request: Request):
    clients = request.app.state.clients
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        for name in SERVICES:
            data = await getattr(clients, name).get("/api/export")
            archive.writestr(f"{name}.json", json.dumps(data, indent=2, sort_keys=True))
        receipt_files = await clients.receipts.get("/api/export/files.zip")
        archive.writestr("receipt-files.zip", receipt_files)
    stamp = dt.date.today().strftime("%Y-%m-%d")
    return Response(
        buffer.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename=sakura-backup-{stamp}.zip"
        },
    )


@router.post("/backup/restore")
async def backup_restore(
    request: Request, file: UploadFile = File(...), confirm: str = Form("")
):
    if confirm != "on":
        return back("/backup", err="Tick the confirmation box — restore replaces ALL data.")
    clients = request.app.state.clients
    raw = await file.read()
    try:
        archive = ZipFile(BytesIO(raw))
    except Exception:  # noqa: BLE001
        return back("/backup", err="That file is not a readable backup zip.")
    names = set(archive.namelist())
    missing = [f"{name}.json" for name in SERVICES if f"{name}.json" not in names]
    if missing:
        return back("/backup", err=f"Backup is missing {', '.join(missing)} — wrong file?")

    restored = []
    for name in SERVICES:
        data = json.loads(archive.read(f"{name}.json"))
        try:
            await getattr(clients, name).post("/api/import", json=data)
        except ServiceError as exc:
            return back(
                "/backup",
                err=f"Restore stopped at {name}: {exc.detail}. Services restored so far: "
                f"{', '.join(restored) or 'none'} — fix the problem and restore again.",
            )
        restored.append(name)
    if "receipt-files.zip" in names:
        try:
            await clients.receipts.request(
                "POST",
                "/api/import/files",
                files={"file": ("receipt-files.zip", archive.read("receipt-files.zip"), "application/zip")},
            )
        except ServiceError as exc:
            return back("/backup", err=f"Data restored, but receipt files failed: {exc.detail}")
    return back("/backup", msg="Restore complete — everything is back.")
