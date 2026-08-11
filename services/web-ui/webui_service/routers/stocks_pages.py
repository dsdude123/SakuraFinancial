from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from ..auth import require_login
from ..clients import ServiceError
from ..rendering import render
from .accounts import back

router = APIRouter(dependencies=[Depends(require_login)])

STOCK_ACCOUNT_TYPES = [
    ("brokerage", "Brokerage (buy & sell freely)"),
    ("rsu", "RSU (vesting grants, trading windows)"),
    ("managed", "Managed portfolio (cash in/out only)"),
]


@router.get("/stocks")
async def stocks_home(request: Request):
    clients = request.app.state.clients
    accounts = await clients.stocks.get("/api/accounts")
    valuation = await clients.stocks.get("/api/valuation")
    securities = await clients.stocks.get("/api/securities")
    return render(
        request,
        "stocks.html",
        {
            "accounts": accounts,
            "valuation": valuation,
            "securities": securities,
            "types": STOCK_ACCOUNT_TYPES,
            "today": dt.date.today().isoformat(),
        },
    )


@router.post("/stocks/accounts")
async def stocks_account_create(
    request: Request,
    name: str = Form(...),
    type: str = Form(...),
    opening_cash: str = Form("0"),
    note: str = Form(""),
):
    try:
        await request.app.state.clients.stocks.post(
            "/api/accounts",
            json={"name": name, "type": type, "opening_cash": opening_cash or "0", "note": note},
        )
    except ServiceError as exc:
        return back("/stocks", err=str(exc.detail))
    return back("/stocks", msg=f"Account '{name}' created")


@router.post("/stocks/prices/refresh")
async def refresh_prices(request: Request):
    try:
        result = await request.app.state.clients.stocks.post("/api/prices/refresh")
    except ServiceError as exc:
        return back("/stocks", err=str(exc.detail))
    updated, failed = len(result.get("updated", [])), result.get("failed", [])
    msg = f"Updated {updated} symbol(s)"
    if failed:
        msg += f"; failed: {', '.join(failed)}"
    return back("/stocks", msg=msg)


@router.post("/stocks/prices/manual")
async def manual_price(
    request: Request, symbol: str = Form(...), date: str = Form(...), close: str = Form(...)
):
    try:
        await request.app.state.clients.stocks.post(
            "/api/prices", json={"symbol": symbol, "date": date, "close": close}
        )
    except ServiceError as exc:
        return back("/stocks", err=str(exc.detail))
    return back("/stocks", msg=f"Price saved for {symbol.upper()}")


@router.get("/stocks/accounts/{account_id}")
async def stock_account_page(request: Request, account_id: int):
    clients = request.app.state.clients
    account = await clients.stocks.get(f"/api/accounts/{account_id}")
    txns = await clients.stocks.get(
        "/api/transactions", params={"account_id": account_id, "limit": 50}
    )
    grants = []
    if account["type"] == "rsu":
        grants = await clients.stocks.get("/api/rsu/grants", params={"account_id": account_id})
    analyses = await clients.stocks.get("/api/analyses", params={"account_id": account_id})
    return render(
        request,
        "stock_account.html",
        {
            "account": account,
            "txns": txns,
            "grants": grants,
            "analyses": analyses[:5],
            "today": dt.date.today().isoformat(),
        },
    )


@router.post("/stocks/accounts/{account_id}/transactions")
async def stock_txn_add(
    request: Request,
    account_id: int,
    type: str = Form(...),
    date: str = Form(...),
    symbol: str = Form(""),
    quantity: str = Form(""),
    price: str = Form(""),
    amount: str = Form(""),
    fees: str = Form(""),
    note: str = Form(""),
):
    body: dict = {"account_id": account_id, "type": type, "date": date, "note": note}
    if symbol.strip():
        body["symbol"] = symbol.strip().upper()
    for field, value in (("quantity", quantity), ("price", price), ("amount", amount), ("fees", fees)):
        if value.strip():
            body[field] = value.strip()
    url = f"/stocks/accounts/{account_id}"
    try:
        await request.app.state.clients.stocks.post("/api/transactions", json=body)
    except ServiceError as exc:
        return back(url, err=str(exc.detail))
    return back(url, msg=f"{type.capitalize()} recorded")


@router.post("/stocks/accounts/{account_id}/grants")
async def grant_create(request: Request, account_id: int):
    form = await request.form()
    vesting = []
    index = 0
    while f"vest_date_{index}" in form:
        vest_date = str(form.get(f"vest_date_{index}", "")).strip()
        shares = str(form.get(f"vest_shares_{index}", "")).strip()
        if vest_date and shares:
            vesting.append({"vest_date": vest_date, "shares": shares})
        index += 1
    url = f"/stocks/accounts/{account_id}"
    try:
        await request.app.state.clients.stocks.post(
            "/api/rsu/grants",
            json={
                "account_id": account_id,
                "symbol": str(form.get("symbol", "")),
                "grant_date": str(form.get("grant_date", "")),
                "note": str(form.get("note", "")),
                "vesting": vesting,
            },
        )
    except ServiceError as exc:
        return back(url, err=str(exc.detail))
    return back(url, msg="Grant recorded")


@router.post("/stocks/vests/{vest_id}/release")
async def release_vest(request: Request, vest_id: int, account_id: int = Form(...), price: str = Form("")):
    body = {"price": price.strip()} if price.strip() else {}
    url = f"/stocks/accounts/{account_id}"
    try:
        await request.app.state.clients.stocks.post(f"/api/rsu/vests/{vest_id}/release", json=body)
    except ServiceError as exc:
        return back(url, err=str(exc.detail))
    return back(url, msg="Shares released into the account")


@router.post("/stocks/accounts/{account_id}/analyze")
async def analyze(request: Request, account_id: int):
    try:
        result = await request.app.state.clients.stocks.post(
            "/api/analyze", json={"account_id": account_id}
        )
    except ServiceError as exc:
        return back(f"/stocks/accounts/{account_id}", err=str(exc.detail))
    return back(
        f"/stocks/analyses/{result['id']}",
    )


@router.get("/stocks/chart")
async def stock_chart_page(request: Request, symbol: str, months: int = 12):
    history = await request.app.state.clients.stocks.get(
        f"/api/prices/{symbol.upper()}", params={"months": months}
    )
    return render(
        request,
        "stock_chart.html",
        {"symbol": symbol.upper(), "months": months, "history": history},
    )


@router.get("/stocks/analyses/{analysis_id}")
async def analysis_view(request: Request, analysis_id: int):
    analysis = await request.app.state.clients.stocks.get(f"/api/analyses/{analysis_id}")
    return render(request, "stock_analysis.html", {"analysis": analysis})


@router.get("/stocks/import")
async def stock_import_page(request: Request, batch: int | None = None, edit: int | None = None):
    clients = request.app.state.clients
    accounts = await clients.stocks.get("/api/accounts")
    profiles = await clients.stocks.get("/api/import/profiles")
    batch_data = None
    if batch is not None:
        batch_data = await clients.stocks.get(f"/api/import/batches/{batch}")
    editing = next((p for p in profiles if p["id"] == edit), None)
    return render(
        request,
        "stock_import.html",
        {"accounts": accounts, "profiles": profiles, "batch": batch_data, "editing": editing},
    )


@router.post("/stocks/import/preview")
async def stock_import_preview(
    request: Request,
    profile_id: int = Form(...),
    account_id: str = Form(""),
    file: UploadFile = File(...),
):
    raw = await file.read()
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        content = raw.decode("latin-1")
    body = {"profile_id": profile_id, "filename": file.filename or "upload.csv", "content": content}
    if account_id.strip():
        body["account_id"] = int(account_id)
    try:
        batch = await request.app.state.clients.stocks.post("/api/import/preview", json=body)
    except ServiceError as exc:
        if isinstance(exc.detail, dict) and exc.detail.get("errors"):
            return render(
                request,
                "import_errors.html",
                {
                    "message": exc.detail.get("message", ""),
                    "errors": exc.detail["errors"],
                    "profile_id": profile_id,
                    "fix_url": f"/stocks/import?edit={profile_id}",
                },
            )
        return back("/stocks/import", err=str(exc.detail))
    return back(f"/stocks/import?batch={batch['id']}")


@router.post("/stocks/import/batches/{batch_id}/commit")
async def stock_import_commit(request: Request, batch_id: int):
    try:
        summary = await request.app.state.clients.stocks.post(
            f"/api/import/batches/{batch_id}/commit"
        )
    except ServiceError as exc:
        return back(f"/stocks/import?batch={batch_id}", err=str(exc.detail))
    return back(
        "/stocks",
        msg=f"Imported {summary['created']} stock transaction(s) "
        f"({', '.join(summary['symbols']) or 'cash only'})",
    )


@router.post("/stocks/import/batches/{batch_id}/abort")
async def stock_import_abort(request: Request, batch_id: int):
    try:
        await request.app.state.clients.stocks.post(f"/api/import/batches/{batch_id}/abort")
    except ServiceError as exc:
        return back("/stocks/import", err=str(exc.detail))
    return back("/stocks/import", msg="Batch discarded")


@router.post("/stocks/import/profiles")
async def stock_profile_save(request: Request):
    form = await request.form()
    import json as jsonlib

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
        except ValueError:
            return back("/stocks/import", err="action map JSON did not parse")
    config = {
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
    body = {
        "name": str(form.get("name") or ""),
        "account_id": int(form["account_id"]) if str(form.get("account_id") or "").strip() else None,
        "config": config,
    }
    profile_id = str(form.get("profile_id") or "").strip()
    try:
        if profile_id:
            await request.app.state.clients.stocks.put(
                f"/api/import/profiles/{profile_id}", json=body
            )
        else:
            await request.app.state.clients.stocks.post("/api/import/profiles", json=body)
    except ServiceError as exc:
        return back("/stocks/import", err=str(exc.detail))
    return back("/stocks/import", msg=f"Profile '{body['name']}' saved")
