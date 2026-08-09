from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Request

from ..auth import require_login
from ..clients import ServiceError
from ..rendering import render

router = APIRouter(dependencies=[Depends(require_login)])


@router.get("/")
async def home(request: Request):
    clients = request.app.state.clients
    accounts = await clients.ledger.get("/api/accounts")
    today = dt.date.today()

    # Pending prompts and upcoming bills; each panel degrades independently.
    try:
        reviews = await clients.ledger.get("/api/bills/occurrences", params={"status": "amount_review"})
        upcoming = await clients.ledger.get(
            "/api/bills/occurrences",
            params={
                "status": "upcoming",
                "start": today.isoformat(),
                "end": (today + dt.timedelta(days=14)).isoformat(),
            },
        )
    except ServiceError:
        reviews, upcoming = [], []
    try:
        uncategorized = await clients.ledger.get(
            "/api/transactions", params={"uncategorized": True, "limit": 100}
        )
    except ServiceError:
        uncategorized = []
    receipts_inbox = None
    try:
        receipts_inbox = await clients.receipts.get("/api/documents", params={"unlinked": True})
    except ServiceError:
        pass  # receipts service optional/down: hide the panel

    groups: dict[str, list] = {}
    for account in accounts:
        groups.setdefault(account["type"], []).append(account)

    return render(
        request,
        "home.html",
        {
            "groups": groups,
            "type_order": ["checking", "savings", "cash", "credit_card", "asset", "liability"],
            "type_labels": {
                "checking": "Checking",
                "savings": "Savings",
                "cash": "Cash",
                "credit_card": "Credit Cards",
                "asset": "Assets",
                "liability": "Liabilities",
            },
            "reviews": reviews,
            "upcoming": upcoming,
            "uncategorized_count": len(uncategorized),
            "receipts_count": len(receipts_inbox) if receipts_inbox is not None else None,
        },
    )
