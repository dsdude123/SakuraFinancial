from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

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
    name: str = Form(""),
    type: str = Form(""),
    opening_cash: str = Form("0"),
    note: str = Form(""),
):
    if not name.strip():
        return back("/stocks", err="Give the account a name.")
    if not type.strip():
        return back("/stocks", err="Choose an account type.")
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
    request: Request, symbol: str = Form(""), date: str = Form(""), close: str = Form("")
):
    missing = [
        label
        for label, value in (("a symbol", symbol), ("a date", date), ("a price", close))
        if not value.strip()
    ]
    if missing:
        return back("/stocks", err=f"A manual price needs {', '.join(missing)}.")
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
    type: str = Form(""),
    date: str = Form(""),
    symbol: str = Form(""),
    quantity: str = Form(""),
    price: str = Form(""),
    amount: str = Form(""),
    fees: str = Form(""),
    note: str = Form(""),
):
    url = f"/stocks/accounts/{account_id}"
    if not type.strip():
        return back(url, err="Choose what kind of transaction this is.")
    if not date.strip():
        return back(url, err="A transaction needs a date.")
    body: dict = {"account_id": account_id, "type": type, "date": date, "note": note}
    if symbol.strip():
        body["symbol"] = symbol.strip().upper()
    for field, value in (("quantity", quantity), ("price", price), ("amount", amount), ("fees", fees)):
        if value.strip():
            body[field] = value.strip()
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
    if not str(form.get("symbol", "")).strip():
        return back(url, err="A grant needs a symbol.")
    if not str(form.get("grant_date", "")).strip():
        return back(url, err="A grant needs a grant date.")
    if not vesting:
        return back(url, err="Add at least one vesting date with a share count.")
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
async def stock_chart_page(request: Request, symbol: str = "", months: str = ""):
    if not symbol.strip():
        return back("/stocks", err="Pick a symbol to chart.")
    try:
        window = int(months) if months.strip() else 12
    except ValueError:
        window = 12  # a junk range in the URL is not worth an error page
    history = await request.app.state.clients.stocks.get(
        f"/api/prices/{symbol.upper()}", params={"months": window}
    )
    return render(
        request,
        "stock_chart.html",
        {"symbol": symbol.upper(), "months": window, "history": history},
    )


@router.get("/stocks/analyses/{analysis_id}")
async def analysis_view(request: Request, analysis_id: int):
    analysis = await request.app.state.clients.stocks.get(f"/api/analyses/{analysis_id}")
    return render(request, "stock_analysis.html", {"analysis": analysis})


@router.get("/stocks/import")
async def stock_import_page():
    """Broker CSVs are imported from the one Import section now, alongside bank
    statements. Kept so old bookmarks land somewhere useful."""
    return RedirectResponse("/import", status_code=301)
