"""The Monthly Updates page: one place to do the monthly chores.

Per month, every active cash-flow account is either **imported** (a committed
CSV batch exists that month), **skipped** (user said "nothing to import"), or
**pending**. Skip flags are stored in the settings service under
``monthly.skip.<yyyy-mm>.<service>.<account_id>`` so they survive restarts and
appear in backups. Stock accounts join the checklist when the stocks service
is running; the receipts inbox and unresolved bill prompts round out the list.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Form, Request

from ..auth import require_login
from ..clients import ServiceError
from ..rendering import render
from .accounts import back
from .budget import month_nav

router = APIRouter(dependencies=[Depends(require_login)])

CASHFLOW_TYPES = ("checking", "savings", "credit_card", "cash")


def skip_key(month: str, service: str, account_id: int) -> str:
    return f"monthly.skip.{month}.{service}.{account_id}"


async def get_skips(request: Request, month: str) -> set[str]:
    rows = await request.app.state.clients.settings.get("/api/settings")
    prefix = f"monthly.skip.{month}."
    return {row["key"] for row in rows if row["key"].startswith(prefix)}


@router.get("/monthly")
async def monthly_page(request: Request, month: str | None = None):
    clients = request.app.state.clients
    month = month or dt.date.today().strftime("%Y-%m")
    accounts = await clients.ledger.get("/api/accounts")
    profiles = await clients.ledger.get("/api/import/profiles")
    batches = await clients.ledger.get("/api/import/batches", params={"month": month})
    skips = await get_skips(request, month)

    committed_accounts = {b["account_id"] for b in batches if b["status"] == "committed"}
    checklist = []
    for account in accounts:
        if account["type"] not in CASHFLOW_TYPES:
            continue
        if skip_key(month, "ledger", account["id"]) in skips:
            status = "skipped"
        elif account["id"] in committed_accounts:
            status = "imported"
        else:
            status = "pending"
        checklist.append(
            {
                "account": account,
                "status": status,
                "profiles": [
                    p for p in profiles if p["account_id"] in (account["id"], None)
                ],
            }
        )

    stock_accounts = None
    try:
        stock_accounts_raw = await clients.stocks.get("/api/accounts")
        stock_batches = await clients.stocks.get("/api/import/batches", params={"month": month})
        stock_profiles = await clients.stocks.get("/api/import/profiles")
        stock_committed = {b["account_id"] for b in stock_batches if b["status"] == "committed"}
        stock_accounts = []
        for account in stock_accounts_raw:
            if skip_key(month, "stocks", account["id"]) in skips:
                status = "skipped"
            elif account["id"] in stock_committed:
                status = "imported"
            else:
                status = "pending"
            stock_accounts.append(
                {
                    "account": account,
                    "status": status,
                    "profiles": [
                        p for p in stock_profiles if p["account_id"] in (account["id"], None)
                    ],
                }
            )
    except ServiceError:
        pass

    receipts_count = None
    try:
        receipts = await clients.receipts.get("/api/documents", params={"unlinked": True})
        receipts_count = len(receipts)
    except ServiceError:
        pass

    try:
        reviews = await clients.ledger.get(
            "/api/bills/occurrences", params={"status": "amount_review"}
        )
    except ServiceError:
        reviews = []

    done = sum(1 for item in checklist if item["status"] != "pending")
    prev_month, next_month = month_nav(month)
    return render(
        request,
        "monthly.html",
        {
            "month": month,
            "prev_month": prev_month,
            "next_month": next_month,
            "checklist": checklist,
            "stock_accounts": stock_accounts,
            "receipts_count": receipts_count,
            "reviews_count": len(reviews),
            "done": done,
            "total": len(checklist),
        },
    )


@router.post("/monthly/{month}/skip")
async def skip_account(
    request: Request, month: str, service: str = Form(...), account_id: int = Form(...)
):
    await request.app.state.clients.settings.put(
        f"/api/settings/{skip_key(month, service, account_id)}",
        json={"value": "1", "is_secret": False},
    )
    return back(f"/monthly?month={month}", msg="Marked as skipped for this month")


@router.post("/monthly/{month}/unskip")
async def unskip_account(
    request: Request, month: str, service: str = Form(...), account_id: int = Form(...)
):
    await request.app.state.clients.settings.delete(
        f"/api/settings/{skip_key(month, service, account_id)}"
    )
    return back(f"/monthly?month={month}", msg="Back on the list")
