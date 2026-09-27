"""The Import section: one place to bring in a CSV, whatever it came from.

Bank statements live in the ledger service and broker activity lives in the
stocks service, but that split is ours, not the user's — they have a pile of
CSVs and one question ("get these into the system"). So every import screen is
shared, and a profile's *kind* ("bank" or "stock") is what decides which
service handles the file:

    kind      profiles                     rows look like
    bank      ledger  /api/import/...      date, description, amount
    stock     stocks  /api/import/...      date, action, symbol, qty, price

Profile and batch ids are namespaced as "<kind>:<id>" in forms and "<kind>/<id>"
in URLs, because the two services number their rows independently and a bare
"3" would be ambiguous.
"""

from __future__ import annotations

import json as jsonlib

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse

from ..auth import require_login
from ..clients import ServiceError
from ..refs import KIND_LABELS, KINDS, split_ref
from ..rendering import render
from .accounts import back

router = APIRouter(dependencies=[Depends(require_login)])

# Re-exported for the templates and tests that import them from here; the
# namespacing itself lives in webui_service.refs, shared with transfers.
__all__ = ["router", "KINDS", "KIND_LABELS", "split_ref"]

STOCK_ACTIONS = ("buy", "sell", "dividend", "vest", "deposit", "withdraw", "fee", "ignore")


def service_for(request: Request, kind: str):
    clients = request.app.state.clients
    return clients.stocks if kind == "stock" else clients.ledger


def decode_upload(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")  # BOM-tolerant; banks love BOMs
    except UnicodeDecodeError:
        return raw.decode("latin-1")


async def load_profiles(request: Request) -> list[dict]:
    """Every profile from both services, tagged with its kind. A stocks service
    that isn't running simply contributes nothing."""
    clients = request.app.state.clients
    profiles = []
    for kind, client, path in (
        ("bank", clients.ledger, "/api/import/profiles"),
        ("stock", clients.stocks, "/api/import/profiles"),
    ):
        try:
            rows = await client.get(path)
        except ServiceError:
            continue
        for row in rows:
            profiles.append({**row, "kind": kind, "ref": f"{kind}:{row['id']}"})
    profiles.sort(key=lambda p: (p["kind"] != "bank", p["name"].lower()))
    return profiles


async def load_accounts(request: Request) -> dict[str, list[dict]]:
    clients = request.app.state.clients
    accounts: dict[str, list[dict]] = {"bank": [], "stock": []}
    try:
        accounts["bank"] = await clients.ledger.get("/api/accounts")
    except ServiceError:
        pass
    try:
        accounts["stock"] = await clients.stocks.get("/api/accounts")
    except ServiceError:
        pass
    return accounts


@router.get("/import")
async def import_start(request: Request):
    clients = request.app.state.clients
    profiles = await load_profiles(request)
    accounts = await load_accounts(request)
    open_batches = []
    for kind, client in (("bank", clients.ledger), ("stock", clients.stocks)):
        try:
            batches = await client.get("/api/import/batches")
        except ServiceError:
            continue
        open_batches.extend(
            {**b, "kind": kind} for b in batches if b["status"] == "review"
        )
    return render(
        request,
        "import_start.html",
        {
            "profiles": profiles,
            "accounts": accounts,
            "open_batches": open_batches,
            "kind_labels": KIND_LABELS,
        },
    )


@router.post("/import/preview")
async def import_preview(
    request: Request,
    profile_id: str = Form(...),
    account_id: str = Form(""),
    file: UploadFile = File(...),
):
    kind, profile_ref = split_ref(profile_id)
    account_kind, account_ref = split_ref(account_id, default_kind=kind)
    if account_ref and account_kind != kind:
        return back(
            "/import",
            err=f"that account doesn't belong to a {KIND_LABELS[kind].lower()} profile "
            "- pick an account of the matching type, or leave it on the profile's own",
        )
    body = {
        "profile_id": int(profile_ref),
        "filename": file.filename or "upload.csv",
        "content": decode_upload(await file.read()),
    }
    if account_ref:
        body["account_id"] = int(account_ref)
    try:
        batch = await service_for(request, kind).post("/api/import/preview", json=body)
    except ServiceError as exc:
        if isinstance(exc.detail, dict) and exc.detail.get("errors"):
            return render(
                request,
                "import_errors.html",
                {
                    "message": exc.detail.get("message", ""),
                    "errors": exc.detail["errors"],
                    "fix_url": f"/import/profiles?edit={kind}:{profile_ref}",
                },
            )
        return back("/import", err=str(exc.detail))
    return back(f"/import/batches/{kind}/{batch['id']}")


@router.get("/import/batches/{batch_id}")
async def batch_review_legacy(batch_id: int):
    """Bookmarks and links from before the two importers were merged."""
    return RedirectResponse(f"/import/batches/bank/{batch_id}", status_code=301)


@router.get("/import/batches/{kind}/{batch_id}")
async def batch_review(request: Request, kind: str, batch_id: int):
    clients = request.app.state.clients
    if kind not in KINDS:
        return back("/import", err=f"unknown import kind {kind!r}")
    batch = await service_for(request, kind).get(f"/api/import/batches/{batch_id}")
    if kind == "stock":
        return render(request, "import_review_stock.html", {"batch": batch})
    payees = await clients.ledger.get("/api/payees")
    categories = await clients.ledger.get("/api/categories")
    accounts = await clients.ledger.get("/api/accounts")
    groups = batch.get("description_groups", [])
    return render(
        request,
        "import_review.html",
        {
            "batch": batch,
            "payees": payees,
            "categories": categories,
            "accounts": accounts,
            "groups": groups,
            "needs_attention": sum(1 for r in batch["rows"] if r["status"] == "needs_payee"),
            "unresolved_groups": sum(1 for g in groups if g["needs_payee"]),
        },
    )


def assignment_from_form(form) -> dict:
    """The payee/category answer shared by the per-row and per-description
    forms — including categories typed rather than picked."""
    body: dict = {}
    if str(form.get("new_payee") or "").strip():
        body["create_payee_name"] = str(form.get("new_payee")).strip()
    elif str(form.get("payee_id") or "").strip():
        body["payee_id"] = int(form["payee_id"])
    if str(form.get("new_category") or "").strip():
        body["create_category_name"] = str(form.get("new_category")).strip()
        kind = str(form.get("new_category_kind") or "").strip()
        if kind:
            body["create_category_kind"] = kind
    elif str(form.get("category_id") or "").strip():
        body["category_id"] = int(form["category_id"])
    return body


@router.post("/import/rows/{row_id}")
async def row_update(request: Request, row_id: int):
    form = await request.form()
    batch_id = str(form.get("batch_id") or "")
    body = assignment_from_form(form)
    body["include"] = form.get("include") == "on"
    if str(form.get("transfer_account_id") or "").strip():
        body["transfer_account_id"] = int(form["transfer_account_id"])
    try:
        await request.app.state.clients.ledger.put(f"/api/import/rows/{row_id}", json=body)
    except ServiceError as exc:
        return back(f"/import/batches/bank/{batch_id}", err=str(exc.detail))
    return back(f"/import/batches/bank/{batch_id}", msg="Row updated")


@router.post("/import/batches/bank/{batch_id}/resolve")
async def resolve_group(request: Request, batch_id: int):
    """Answer every row sharing one bank description in a single submit."""
    form = await request.form()
    body = assignment_from_form(form)
    body["description"] = str(form.get("description") or "")
    if not (body.get("payee_id") or body.get("create_payee_name") or body.get("category_id")
            or body.get("create_category_name")):
        return back(
            f"/import/batches/bank/{batch_id}",
            err="pick a payee or a category before applying it to the group",
        )
    try:
        result = await request.app.state.clients.ledger.post(
            f"/api/import/batches/{batch_id}/resolve", json=body
        )
    except ServiceError as exc:
        return back(f"/import/batches/bank/{batch_id}", err=str(exc.detail))
    return back(
        f"/import/batches/bank/{batch_id}",
        msg=f"Applied to {result['updated']} row(s) matching \"{result['description']}\"",
    )


@router.post("/import/stock-rows/{row_id}")
async def stock_row_update(request: Request, row_id: int, batch_id: int = Form(...),
                           include: str = Form("")):
    try:
        await request.app.state.clients.stocks.put(
            f"/api/import/rows/{row_id}", json={"include": include == "on"}
        )
    except ServiceError as exc:
        return back(f"/import/batches/stock/{batch_id}", err=str(exc.detail))
    return back(f"/import/batches/stock/{batch_id}", msg="Row updated")


@router.post("/import/batches/bank/{batch_id}/reclassify")
async def batch_reclassify(request: Request, batch_id: int):
    """Re-run the batch against the rules as they stand now, so an alias added
    mid-review takes effect without discarding and re-uploading the file."""
    try:
        result = await request.app.state.clients.ledger.post(
            f"/api/import/batches/{batch_id}/reclassify"
        )
    except ServiceError as exc:
        return back(f"/import/batches/bank/{batch_id}", err=str(exc.detail))
    if not result["changed"]:
        return back(
            f"/import/batches/bank/{batch_id}",
            msg="Re-scanned: no unanswered row matched anything new.",
        )
    return back(
        f"/import/batches/bank/{batch_id}",
        msg=f"Re-scanned: {result['changed']} row(s) picked up a payee or rule.",
    )


@router.post("/import/batches/{kind}/{batch_id}/commit")
async def batch_commit(
    request: Request, kind: str, batch_id: int, name_from_description: str = Form("")
):
    if kind not in KINDS:
        return back("/import", err=f"unknown import kind {kind!r}")
    params = {}
    if kind == "bank":
        params["name_payees_from_descriptions"] = name_from_description == "on"
    try:
        summary = await service_for(request, kind).request(
            "POST", f"/api/import/batches/{batch_id}/commit", params=params
        )
    except ServiceError as exc:
        return back(f"/import/batches/{kind}/{batch_id}", err=str(exc.detail))
    if kind == "stock":
        return back(
            "/stocks",
            msg=f"Imported {summary['created']} stock transaction(s) "
            f"({', '.join(summary['symbols']) or 'cash only'})",
        )
    return render(request, "import_summary.html", {"summary": summary})


@router.post("/import/batches/{kind}/{batch_id}/abort")
async def batch_abort(request: Request, kind: str, batch_id: int):
    if kind not in KINDS:
        return back("/import", err=f"unknown import kind {kind!r}")
    try:
        await service_for(request, kind).post(f"/api/import/batches/{batch_id}/abort")
    except ServiceError as exc:
        return back("/import", err=str(exc.detail))
    return back("/import", msg="Batch discarded - nothing was imported")


@router.get("/import/profiles")
async def profiles_page(request: Request, edit: str | None = None, kind: str = "bank"):
    profiles = await load_profiles(request)
    accounts = await load_accounts(request)
    editing = next((p for p in profiles if p["ref"] == edit), None) if edit else None
    form_kind = editing["kind"] if editing else (kind if kind in KINDS else "bank")
    return render(
        request,
        "profiles.html",
        {
            "profiles": profiles,
            "accounts": accounts,
            "editing": editing,
            "form_kind": form_kind,
            "kind_labels": KIND_LABELS,
            "stock_actions": STOCK_ACTIONS,
        },
    )


def bank_config_from_form(form) -> dict:
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


def stock_config_from_form(form) -> dict:
    action_map: dict = {}
    index = 0
    while f"map_from_{index}" in form:
        source = str(form.get(f"map_from_{index}", "")).strip()
        target = str(form.get(f"map_to_{index}", "")).strip()
        if source and target:
            action_map[source] = target
        index += 1
    raw_map = str(form.get("action_map_json", "")).strip()
    if raw_map:
        try:
            action_map.update(jsonlib.loads(raw_map))
        except ValueError as exc:
            raise ValueError(
                'the extra action map must be JSON like {"Reinvest": "buy"}'
            ) from exc
    return {
        "delimiter": str(form.get("delimiter") or ","),
        "has_header": form.get("has_header") == "on",
        "skip_top_rows": int(form.get("skip_top_rows") or 0),
        "date_column": str(form.get("date_column") or ""),
        "date_format": str(form.get("date_format") or "%m/%d/%Y"),
        "action_column": str(form.get("action_column") or ""),
        "symbol_column": str(form.get("symbol_column") or ""),
        "quantity_column": str(form.get("quantity_column") or ""),
        "price_column": str(form.get("price_column") or ""),
        "fee_column": str(form.get("fee_column") or ""),
        "amount_column": str(form.get("amount_column") or ""),
        "description_column": str(form.get("description_column") or ""),
        "action_map": action_map,
    }


async def save_profile(request: Request, kind: str, profile_id: str | None):
    form = await request.form()
    try:
        config = stock_config_from_form(form) if kind == "stock" else bank_config_from_form(form)
    except ValueError as exc:
        message = str(exc) if "JSON" in str(exc) else '"skip top rows" must be a whole number'
        return back(f"/import/profiles?kind={kind}", err=message)
    account_id = str(form.get("account_id") or "").strip()
    body = {
        "name": str(form.get("name") or ""),
        "account_id": int(split_ref(account_id, kind)[1]) if account_id else None,
        "config": config,
    }
    client = service_for(request, kind)
    try:
        if profile_id:
            await client.put(f"/api/import/profiles/{profile_id}", json=body)
        else:
            await client.post("/api/import/profiles", json=body)
    except ServiceError as exc:
        where = f"?edit={kind}:{profile_id}" if profile_id else f"?kind={kind}"
        return back(f"/import/profiles{where}", err=str(exc.detail))
    return back("/import/profiles", msg=f"Profile '{body['name']}' saved")


@router.post("/import/profiles")
async def profile_create(request: Request):
    form = await request.form()
    kind = str(form.get("kind") or "bank")
    return await save_profile(request, kind if kind in KINDS else "bank", None)


@router.post("/import/profiles/{kind}/{profile_id}")
async def profile_update(request: Request, kind: str, profile_id: int):
    if kind not in KINDS:
        return back("/import/profiles", err=f"unknown profile kind {kind!r}")
    return await save_profile(request, kind, str(profile_id))


@router.post("/import/profiles/{kind}/{profile_id}/delete")
async def profile_delete(request: Request, kind: str, profile_id: int):
    if kind not in KINDS:
        return back("/import/profiles", err=f"unknown profile kind {kind!r}")
    try:
        await service_for(request, kind).delete(f"/api/import/profiles/{profile_id}")
    except ServiceError as exc:
        return back("/import/profiles", err=str(exc.detail))
    return back("/import/profiles", msg="Profile deleted")
