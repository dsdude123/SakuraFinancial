from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Form, Request

from ..auth import require_login
from ..clients import ServiceError
from ..rendering import render
from .accounts import back

router = APIRouter(dependencies=[Depends(require_login)])


def month_nav(month: str) -> tuple[str, str]:
    year, mon = (int(part) for part in month.split("-"))
    prev = (year - 1, 12) if mon == 1 else (year, mon - 1)
    nxt = (year + 1, 1) if mon == 12 else (year, mon + 1)
    return f"{prev[0]:04d}-{prev[1]:02d}", f"{nxt[0]:04d}-{nxt[1]:02d}"


@router.get("/budget")
async def budget_page(request: Request, month: str | None = None):
    clients = request.app.state.clients
    month = month or dt.date.today().strftime("%Y-%m")
    view = await clients.budget.get(f"/api/budget/{month}")
    categories = await clients.ledger.get("/api/categories")
    # Rows that already have an editable envelope shouldn't be offered again;
    # subtotal rows can still be given a budget of their own.
    has_envelope = {
        row["category_id"]
        for row in view["categories"]
        if row["category_id"] and not row.get("is_subtotal")
    }
    addable = [
        c
        for c in categories
        if c["kind"] == "expense" and c["id"] not in has_envelope and c["active"]
    ]
    prev_month, next_month = month_nav(month)
    return render(
        request,
        "budget.html",
        {
            "view": view,
            "month": month,
            "prev_month": prev_month,
            "next_month": next_month,
            "addable": addable,
        },
    )


@router.post("/budget/{month}/set")
async def budget_set(request: Request, month: str, category_id: int = Form(...), amount: str = Form("0")):
    try:
        await request.app.state.clients.budget.put(
            f"/api/budget/{month}/categories/{category_id}", json={"amount": amount or "0"}
        )
    except ServiceError as exc:
        return back(f"/budget?month={month}", err=str(exc.detail))
    return back(f"/budget?month={month}", msg="Budget saved")


@router.post("/budget/{month}/copy-previous")
async def budget_copy(request: Request, month: str):
    try:
        result = await request.app.state.clients.budget.post(
            f"/api/budget/{month}/copy-from-previous"
        )
    except ServiceError as exc:
        return back(f"/budget?month={month}", err=str(exc.detail))
    return back(f"/budget?month={month}", msg=f"Copied {result['copied']} budgets from last month")


@router.get("/goals")
async def goals_page(request: Request):
    goals = await request.app.state.clients.budget.get(
        "/api/goals", params={"include_inactive": True}
    )
    month = dt.date.today().strftime("%Y-%m")
    try:
        view = await request.app.state.clients.budget.get(f"/api/budget/{month}")
        progress = {g["goal_id"]: g for g in view["waterfall"]["goals"]}
    except ServiceError:
        progress = {}
    return render(request, "goals.html", {"goals": goals, "progress": progress})


@router.post("/goals")
async def goal_create(
    request: Request,
    name: str = Form(...),
    target_amount: str = Form(...),
    monthly_contribution: str = Form(...),
    target_date: str = Form(""),
    priority: int = Form(1),
):
    body = {
        "name": name,
        "target_amount": target_amount,
        "monthly_contribution": monthly_contribution,
        "priority": priority,
    }
    if target_date.strip():
        body["target_date"] = target_date.strip()
    try:
        await request.app.state.clients.budget.post("/api/goals", json=body)
    except ServiceError as exc:
        return back("/goals", err=str(exc.detail))
    return back("/goals", msg=f"Goal '{name}' created")


@router.post("/goals/{goal_id}/update")
async def goal_update(
    request: Request,
    goal_id: int,
    monthly_contribution: str = Form(""),
    priority: str = Form(""),
    active: str = Form(""),
):
    body: dict = {"active": active == "on"}
    if monthly_contribution.strip():
        body["monthly_contribution"] = monthly_contribution.strip()
    if priority.strip():
        body["priority"] = int(priority)
    try:
        await request.app.state.clients.budget.put(f"/api/goals/{goal_id}", json=body)
    except ServiceError as exc:
        return back("/goals", err=str(exc.detail))
    return back("/goals", msg="Goal updated")
