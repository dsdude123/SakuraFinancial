from __future__ import annotations

import datetime as dt
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from ..auth import require_login
from ..clients import ServiceError
from ..rendering import render

router = APIRouter(dependencies=[Depends(require_login)])

ACCOUNT_TYPES = [
    ("checking", "Checking"),
    ("savings", "Savings"),
    ("credit_card", "Credit card"),
    ("cash", "Cash (physical money, any currency)"),
    ("asset", "Asset (car, house...)"),
    ("liability", "Liability (loan...)"),
]


def back(url: str, msg: str = "", err: str = "") -> RedirectResponse:
    if msg:
        url += ("&" if "?" in url else "?") + "msg=" + quote(msg)
    if err:
        url += ("&" if "?" in url else "?") + "err=" + quote(err)
    return RedirectResponse(url, status_code=303)


@router.get("/accounts")
async def accounts_list(request: Request):
    clients = request.app.state.clients
    accounts = await clients.ledger.get("/api/accounts", params={"include_inactive": True})
    currencies = await clients.ledger.get("/api/currencies")
    return render(
        request,
        "accounts_list.html",
        {"accounts": accounts, "currencies": currencies, "types": ACCOUNT_TYPES},
    )


@router.post("/accounts")
async def account_create(
    request: Request,
    name: str = Form(...),
    type: str = Form(...),
    currency_code: str = Form("USD"),
    opening_balance: str = Form("0"),
    note: str = Form(""),
):
    try:
        await request.app.state.clients.ledger.post(
            "/api/accounts",
            json={
                "name": name,
                "type": type,
                "currency_code": currency_code,
                "opening_balance": opening_balance or "0",
                "note": note,
            },
        )
    except ServiceError as exc:
        return back("/accounts", err=str(exc.detail))
    return back("/accounts", msg=f"Account '{name}' created")


@router.post("/accounts/{account_id}/update")
async def account_update(
    request: Request,
    account_id: int,
    name: str = Form(...),
    opening_balance: str = Form("0"),
    note: str = Form(""),
    active: str = Form(""),
):
    try:
        await request.app.state.clients.ledger.put(
            f"/api/accounts/{account_id}",
            json={
                "name": name,
                "opening_balance": opening_balance or "0",
                "note": note,
                "active": active == "on",
            },
        )
    except ServiceError as exc:
        return back("/accounts", err=str(exc.detail))
    return back("/accounts", msg="Account updated")


@router.get("/accounts/{account_id}/register")
async def register(request: Request, account_id: int, offset: int = 0, prefill_payee: int | None = None):
    clients = request.app.state.clients
    account = await clients.ledger.get(f"/api/accounts/{account_id}")
    limit = 50
    txns = await clients.ledger.get(
        "/api/transactions",
        params={"account_id": account_id, "limit": limit, "offset": offset},
    )
    payees = await clients.ledger.get("/api/payees")
    categories = await clients.ledger.get("/api/categories")
    accounts = await clients.ledger.get("/api/accounts")

    # MS Money-style auto-fill: picking a payee and pressing "Fill" re-renders
    # the entry form with the payee's default category and last amount.
    prefill = None
    if prefill_payee:
        try:
            prefill = await clients.ledger.get(f"/api/payees/{prefill_payee}")
        except ServiceError:
            prefill = None

    # Running balance: newest-first list, so walk from the account balance down.
    from decimal import Decimal

    running = Decimal(account["balance"])
    for txn in txns:
        txn["running_balance"] = str(running)
        running -= Decimal(txn["total"])

    return render(
        request,
        "register.html",
        {
            "account": account,
            "txns": txns,
            "payees": payees,
            "categories": categories,
            "accounts": [a for a in accounts if a["id"] != account_id],
            "prefill": prefill,
            "today": dt.date.today().isoformat(),
            "offset": offset,
            "limit": limit,
        },
    )


@router.post("/accounts/{account_id}/register")
async def register_add(
    request: Request,
    account_id: int,
    date: str = Form(...),
    payee_id: str = Form(""),
    new_payee: str = Form(""),
    category_id: str = Form(""),
    amount: str = Form(...),
    memo: str = Form(""),
):
    clients = request.app.state.clients
    url = f"/accounts/{account_id}/register"
    try:
        chosen_payee = int(payee_id) if payee_id else None
        if new_payee.strip():
            created = await clients.ledger.post(
                "/api/payees",
                json={
                    "name": new_payee.strip(),
                    "default_category_id": int(category_id) if category_id else None,
                },
            )
            chosen_payee = created["id"]
        result = await clients.ledger.post(
            "/api/transactions",
            json={
                "account_id": account_id,
                "date": date,
                "payee_id": chosen_payee,
                "memo": memo,
                "splits": [
                    {
                        "category_id": int(category_id) if category_id else None,
                        "amount": amount,
                    }
                ],
            },
        )
    except (ServiceError, ValueError) as exc:
        detail = exc.detail if isinstance(exc, ServiceError) else str(exc)
        return back(url, err=f"Not saved: {detail}")
    matches = result.get("bill_matches", [])
    if matches and matches[0]["status"] == "amount_review":
        return back(
            "/bills",
            msg=f"Saved. '{matches[0]['bill_name']}' was charged a different amount — decide below.",
        )
    if matches:
        return back(url, msg=f"Saved and matched to bill '{matches[0]['bill_name']}'")
    return back(url, msg="Transaction saved")


@router.post("/accounts/{account_id}/transfer")
async def transfer_add(
    request: Request,
    account_id: int,
    date: str = Form(...),
    direction: str = Form("out"),
    other_account_id: int = Form(...),
    amount: str = Form(...),
    to_amount: str = Form(""),
    memo: str = Form(""),
):
    clients = request.app.state.clients
    url = f"/accounts/{account_id}/register"
    from_id, to_id = (
        (account_id, other_account_id) if direction == "out" else (other_account_id, account_id)
    )
    body = {
        "from_account_id": from_id,
        "to_account_id": to_id,
        "date": date,
        "amount": amount,
        "memo": memo,
    }
    if to_amount.strip():
        body["to_amount"] = to_amount.strip()
    try:
        await clients.ledger.post("/api/transfers", json=body)
    except ServiceError as exc:
        return back(url, err=f"Transfer not saved: {exc.detail}")
    return back(url, msg="Transfer saved")


@router.post("/accounts/{account_id}/valuation")
async def valuation_add(
    request: Request, account_id: int, date: str = Form(...), new_value: str = Form(...)
):
    url = f"/accounts/{account_id}/register"
    try:
        await request.app.state.clients.ledger.post(
            f"/api/accounts/{account_id}/valuation",
            json={"date": date, "new_value": new_value},
        )
    except ServiceError as exc:
        return back(url, err=f"Not saved: {exc.detail}")
    return back(url, msg="Value updated — net worth reflects it; cash flow won't")


@router.get("/transactions/{transaction_id}/edit")
async def txn_edit_page(request: Request, transaction_id: int, rows: int = 0):
    clients = request.app.state.clients
    txn = await clients.ledger.get(f"/api/transactions/{transaction_id}")
    categories = await clients.ledger.get("/api/categories")
    payees = await clients.ledger.get("/api/payees")
    split_rows = max(rows, len(txn["splits"]), 1)
    return render(
        request,
        "txn_edit.html",
        {
            "txn": txn,
            "categories": categories,
            "payees": payees,
            "split_rows": split_rows,
        },
    )


@router.post("/transactions/{transaction_id}/edit")
async def txn_edit_submit(request: Request, transaction_id: int):
    clients = request.app.state.clients
    form = await request.form()
    txn = await clients.ledger.get(f"/api/transactions/{transaction_id}")
    url = f"/accounts/{txn['account_id']}/register"

    if "action_add" in form:
        current = int(form.get("split_rows", "1"))
        return RedirectResponse(
            f"/transactions/{transaction_id}/edit?rows={current + 1}", status_code=303
        )
    if "action_delete" in form:
        await clients.ledger.delete(f"/api/transactions/{transaction_id}")
        return back(url, msg="Transaction deleted")

    splits = []
    index = 0
    while f"split_amount_{index}" in form:
        amount = str(form.get(f"split_amount_{index}", "")).strip()
        if amount:
            category = str(form.get(f"split_category_{index}", "")).strip()
            splits.append(
                {
                    "category_id": int(category) if category else None,
                    "amount": amount,
                    "memo": str(form.get(f"split_memo_{index}", "")),
                }
            )
        index += 1
    body = {
        "date": str(form.get("date")),
        "memo": str(form.get("memo", "")),
        "status": str(form.get("status", "uncleared")),
    }
    if txn["kind"] == "normal":
        payee = str(form.get("payee_id", "")).strip()
        if payee:
            body["payee_id"] = int(payee)
        if splits:
            body["splits"] = splits
    try:
        await clients.ledger.put(f"/api/transactions/{transaction_id}", json=body)
    except ServiceError as exc:
        return back(f"/transactions/{transaction_id}/edit", err=str(exc.detail))
    return back(url, msg="Transaction updated")
