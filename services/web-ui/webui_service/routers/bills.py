from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Form, Request

from ..auth import require_login
from ..clients import ServiceError
from ..rendering import render
from .accounts import back

router = APIRouter(dependencies=[Depends(require_login)])

FREQUENCIES = ["weekly", "biweekly", "monthly", "quarterly", "semiannual", "annual"]


@router.get("/bills")
async def bills_page(request: Request):
    clients = request.app.state.clients
    accrual = await clients.ledger.get("/api/bills/accrual")
    reviews = await clients.ledger.get("/api/bills/occurrences", params={"status": "amount_review"})
    today = dt.date.today()
    upcoming = await clients.ledger.get(
        "/api/bills/occurrences",
        params={
            "status": "upcoming",
            "start": today.isoformat(),
            "end": (today + dt.timedelta(days=60)).isoformat(),
        },
    )
    payees = await clients.ledger.get("/api/payees")
    categories = await clients.ledger.get("/api/categories")
    bills = await clients.ledger.get("/api/bills", params={"include_inactive": True})
    return render(
        request,
        "bills.html",
        {
            "accrual": accrual,
            "reviews": reviews,
            "upcoming": upcoming,
            "bills": bills,
            "payees": payees,
            "categories": categories,
            "frequencies": FREQUENCIES,
            "today": today.isoformat(),
        },
    )


@router.post("/bills")
async def bill_create(
    request: Request,
    name: str = Form(...),
    payee_id: int = Form(...),
    category_id: str = Form(""),
    frequency: str = Form("monthly"),
    amount: str = Form(...),
    is_variable: str = Form(""),
    next_due: str = Form(...),
):
    try:
        await request.app.state.clients.ledger.post(
            "/api/bills",
            json={
                "name": name,
                "payee_id": payee_id,
                "category_id": int(category_id) if category_id else None,
                "frequency": frequency,
                "amount": amount,
                "is_variable": is_variable == "on",
                "next_due": next_due,
            },
        )
    except ServiceError as exc:
        return back("/bills", err=f"Bill not saved: {exc.detail}")
    return back("/bills", msg=f"Bill '{name}' created")


@router.post("/bills/{bill_id}/update")
async def bill_update(
    request: Request,
    bill_id: int,
    amount: str = Form(""),
    is_variable: str = Form(""),
    active: str = Form(""),
):
    body: dict = {"is_variable": is_variable == "on", "active": active == "on"}
    if amount.strip():
        body["amount"] = amount.strip()
    try:
        await request.app.state.clients.ledger.put(f"/api/bills/{bill_id}", json=body)
    except ServiceError as exc:
        return back("/bills", err=str(exc.detail))
    return back("/bills", msg="Bill updated")


@router.post("/bills/occurrences/{occurrence_id}/resolve")
async def resolve(request: Request, occurrence_id: int, action: str = Form(...)):
    try:
        result = await request.app.state.clients.ledger.post(
            f"/api/bills/occurrences/{occurrence_id}/resolve", json={"action": action}
        )
    except ServiceError as exc:
        return back("/bills", err=str(exc.detail))
    if action == "update_bill":
        return back("/bills", msg=f"'{result['bill_name']}' updated to {result['actual_amount']}")
    return back("/bills", msg=f"Kept the old amount for '{result['bill_name']}'")


@router.post("/bills/occurrences/{occurrence_id}/skip")
async def skip(request: Request, occurrence_id: int):
    try:
        await request.app.state.clients.ledger.post(f"/api/bills/occurrences/{occurrence_id}/skip")
    except ServiceError as exc:
        return back("/bills", err=str(exc.detail))
    return back("/bills", msg="Occurrence skipped")
