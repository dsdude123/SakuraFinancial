from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from ..auth import require_login
from ..clients import ServiceError
from ..rendering import render
from .accounts import back

router = APIRouter(dependencies=[Depends(require_login)])


@router.get("/import")
async def import_start(request: Request):
    clients = request.app.state.clients
    accounts = await clients.ledger.get("/api/accounts")
    profiles = await clients.ledger.get("/api/import/profiles")
    batches = await clients.ledger.get("/api/import/batches")
    open_batches = [b for b in batches if b["status"] == "review"]
    return render(
        request,
        "import_start.html",
        {"accounts": accounts, "profiles": profiles, "open_batches": open_batches},
    )


@router.post("/import/preview")
async def import_preview(
    request: Request,
    profile_id: int = Form(...),
    account_id: str = Form(""),
    file: UploadFile = File(...),
):
    clients = request.app.state.clients
    raw = await file.read()
    try:
        content = raw.decode("utf-8-sig")  # BOM-tolerant; banks love BOMs
    except UnicodeDecodeError:
        content = raw.decode("latin-1")
    body = {"profile_id": profile_id, "filename": file.filename or "upload.csv", "content": content}
    if account_id.strip():
        body["account_id"] = int(account_id)
    try:
        batch = await clients.ledger.post("/api/import/preview", json=body)
    except ServiceError as exc:
        if isinstance(exc.detail, dict) and exc.detail.get("errors"):
            return render(
                request,
                "import_errors.html",
                {
                    "message": exc.detail.get("message", ""),
                    "errors": exc.detail["errors"],
                    "profile_id": profile_id,
                },
            )
        return back("/import", err=str(exc.detail))
    return back(f"/import/batches/{batch['id']}")


@router.get("/import/batches/{batch_id}")
async def batch_review(request: Request, batch_id: int):
    clients = request.app.state.clients
    batch = await clients.ledger.get(f"/api/import/batches/{batch_id}")
    payees = await clients.ledger.get("/api/payees")
    categories = await clients.ledger.get("/api/categories")
    accounts = await clients.ledger.get("/api/accounts")
    needs_attention = sum(1 for r in batch["rows"] if r["status"] == "needs_payee")
    return render(
        request,
        "import_review.html",
        {
            "batch": batch,
            "payees": payees,
            "categories": categories,
            "accounts": accounts,
            "needs_attention": needs_attention,
        },
    )


@router.post("/import/rows/{row_id}")
async def row_update(
    request: Request,
    row_id: int,
    batch_id: int = Form(...),
    payee_id: str = Form(""),
    new_payee: str = Form(""),
    category_id: str = Form(""),
    include: str = Form(""),
    transfer_account_id: str = Form(""),
):
    body: dict = {"include": include == "on"}
    if new_payee.strip():
        body["create_payee_name"] = new_payee.strip()
    elif payee_id.strip():
        body["payee_id"] = int(payee_id)
    if category_id.strip():
        body["category_id"] = int(category_id)
    if transfer_account_id.strip():
        body["transfer_account_id"] = int(transfer_account_id)
    try:
        await request.app.state.clients.ledger.put(f"/api/import/rows/{row_id}", json=body)
    except ServiceError as exc:
        return back(f"/import/batches/{batch_id}", err=str(exc.detail))
    return back(f"/import/batches/{batch_id}", msg="Row updated")


@router.post("/import/batches/{batch_id}/commit")
async def batch_commit(request: Request, batch_id: int):
    clients = request.app.state.clients
    try:
        summary = await clients.ledger.post(f"/api/import/batches/{batch_id}/commit")
    except ServiceError as exc:
        return back(f"/import/batches/{batch_id}", err=str(exc.detail))
    return render(request, "import_summary.html", {"summary": summary})


@router.post("/import/batches/{batch_id}/abort")
async def batch_abort(request: Request, batch_id: int):
    try:
        await request.app.state.clients.ledger.post(f"/api/import/batches/{batch_id}/abort")
    except ServiceError as exc:
        return back("/import", err=str(exc.detail))
    return back("/import", msg="Batch discarded — nothing was imported")


@router.get("/import/profiles")
async def profiles_page(request: Request, edit: int | None = None):
    clients = request.app.state.clients
    profiles = await clients.ledger.get("/api/import/profiles")
    accounts = await clients.ledger.get("/api/accounts")
    editing = next((p for p in profiles if p["id"] == edit), None)
    return render(
        request,
        "profiles.html",
        {"profiles": profiles, "accounts": accounts, "editing": editing},
    )


def config_from_form(form) -> dict:
    config = {
        "delimiter": str(form.get("delimiter") or ","),
        "has_header": form.get("has_header") == "on",
        "skip_top_rows": int(form.get("skip_top_rows") or 0),
        "date_column": str(form.get("date_column") or ""),
        "date_format": str(form.get("date_format") or "%m/%d/%Y"),
        "description_column": str(form.get("description_column") or ""),
        "amount_mode": str(form.get("amount_mode") or "single"),
        "negate_amount": form.get("negate_amount") == "on",
    }
    if str(form.get("memo_column") or "").strip():
        config["memo_column"] = str(form.get("memo_column")).strip()
    if config["amount_mode"] == "single":
        config["amount_column"] = str(form.get("amount_column") or "")
    else:
        config["debit_column"] = str(form.get("debit_column") or "")
        config["credit_column"] = str(form.get("credit_column") or "")
    return config


@router.post("/import/profiles")
async def profile_create(request: Request):
    form = await request.form()
    body = {
        "name": str(form.get("name") or ""),
        "account_id": int(form["account_id"]) if str(form.get("account_id") or "").strip() else None,
        "config": config_from_form(form),
    }
    try:
        await request.app.state.clients.ledger.post("/api/import/profiles", json=body)
    except ServiceError as exc:
        return back("/import/profiles", err=str(exc.detail))
    return back("/import/profiles", msg=f"Profile '{body['name']}' saved")


@router.post("/import/profiles/{profile_id}")
async def profile_update(request: Request, profile_id: int):
    form = await request.form()
    body = {
        "name": str(form.get("name") or ""),
        "account_id": int(form["account_id"]) if str(form.get("account_id") or "").strip() else None,
        "config": config_from_form(form),
    }
    try:
        await request.app.state.clients.ledger.put(
            f"/api/import/profiles/{profile_id}", json=body
        )
    except ServiceError as exc:
        return back(f"/import/profiles?edit={profile_id}", err=str(exc.detail))
    return back("/import/profiles", msg="Profile updated")
