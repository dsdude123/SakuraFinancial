"""Transaction search with bulk editing.

The ledger has always been able to filter transactions; what was missing was a
screen for it, and any way to act on the results as a set. Re-filing a year of
imported rows one at a time is the job this page exists to avoid.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request

from ..auth import require_login
from ..clients import ServiceError
from ..rendering import render
from .accounts import back

router = APIRouter(dependencies=[Depends(require_login)])

PAGE_SIZE = 200


def query_string(form) -> str:
    """Rebuild the search URL so a bulk edit returns to the same result set."""
    keep = ("q", "account_id", "category_id", "payee_id", "start", "end", "uncategorized")
    parts = [f"{key}={form.get(key)}" for key in keep if str(form.get(key) or "").strip()]
    return "/search" + ("?" + "&".join(parts) if parts else "")


@router.get("/search")
async def search_page(
    request: Request,
    q: str = "",
    account_id: str = "",
    category_id: str = "",
    payee_id: str = "",
    start: str = "",
    end: str = "",
    uncategorized: str = "",
    offset: int = 0,
):
    clients = request.app.state.clients
    accounts = await clients.ledger.get("/api/accounts", params={"include_inactive": True})
    categories = await clients.ledger.get("/api/categories")
    payees = await clients.ledger.get("/api/payees")

    params: dict = {"limit": PAGE_SIZE, "offset": max(offset, 0)}
    for name, value in (
        ("q", q),
        ("account_id", account_id),
        ("category_id", category_id),
        ("payee_id", payee_id),
        ("start", start),
        ("end", end),
    ):
        if str(value).strip():
            params[name] = value.strip()
    if uncategorized == "on":
        params["uncategorized"] = True

    searched = bool(params.keys() - {"limit", "offset"})
    results = []
    if searched:
        try:
            results = await clients.ledger.get("/api/transactions", params=params)
        except ServiceError as exc:
            return back("/search", err=str(exc.detail))

    filters = {
        "q": q,
        "account_id": account_id,
        "category_id": category_id,
        "payee_id": payee_id,
        "start": start,
        "end": end,
        "uncategorized": uncategorized,
    }
    return render(
        request,
        "search.html",
        {
            "accounts": accounts,
            "categories": categories,
            "payees": payees,
            "results": results,
            "searched": searched,
            "filters": filters,
            "offset": max(offset, 0),
            "page_size": PAGE_SIZE,
            "more": len(results) == PAGE_SIZE,
        },
    )


@router.post("/search/bulk")
async def bulk_edit(request: Request):
    form = await request.form()
    back_to = query_string(form)
    ids = [int(value) for value in form.getlist("txn") if str(value).strip().isdigit()]
    if not ids:
        return back(back_to, err="Tick the transactions you want to change first.")

    # "apply_*" so the change being made is never confused with the filter
    # fields carried alongside it to rebuild the search.
    body: dict = {"transaction_ids": ids}
    if str(form.get("apply_category_id") or "").strip():
        body["category_id"] = int(form["apply_category_id"])
    if str(form.get("apply_payee_id") or "").strip():
        body["payee_id"] = int(form["apply_payee_id"])
    if str(form.get("apply_status") or "").strip():
        body["status"] = str(form["apply_status"]).strip()
    if len(body) == 1:
        return back(back_to, err="Choose a category, a payee or a status to apply.")

    try:
        result = await request.app.state.clients.ledger.post("/api/transactions/bulk", json=body)
    except ServiceError as exc:
        return back(back_to, err=str(exc.detail))

    message = f"Updated {result['updated']} transaction(s)"
    if result["skipped"]:
        # Say what was left alone and why, rather than quietly doing less than
        # the user asked for.
        first = result["skipped"][0]
        message += (
            f"; {len(result['skipped'])} left alone "
            f"(e.g. #{first['id']} because {first['reason']})"
        )
    return back(back_to, msg=message + ".")
