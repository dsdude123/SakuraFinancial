"""Payee and category administration pages."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request

from ..auth import require_login
from ..clients import ServiceError
from ..rendering import render
from .accounts import back

router = APIRouter(dependencies=[Depends(require_login)])


@router.get("/payees")
async def payees_page(request: Request, show: int | None = None):
    clients = request.app.state.clients
    payees = await clients.ledger.get("/api/payees", params={"include_inactive": True})
    categories = await clients.ledger.get("/api/categories")
    aliases = None
    shown = None
    if show:
        shown = next((p for p in payees if p["id"] == show), None)
        if shown:
            aliases = await clients.ledger.get(f"/api/payees/{show}/aliases")
    return render(
        request,
        "payees.html",
        {"payees": payees, "categories": categories, "shown": shown, "aliases": aliases},
    )


@router.post("/payees")
async def payee_create(
    request: Request, name: str = Form(...), default_category_id: str = Form("")
):
    body = {"name": name}
    if default_category_id.strip():
        body["default_category_id"] = int(default_category_id)
    try:
        await request.app.state.clients.ledger.post("/api/payees", json=body)
    except ServiceError as exc:
        return back("/payees", err=str(exc.detail))
    return back("/payees", msg=f"Payee '{name}' created")


@router.post("/payees/{payee_id}/update")
async def payee_update(
    request: Request,
    payee_id: int,
    default_category_id: str = Form(""),
    active: str = Form(""),
):
    body: dict = {"active": active == "on"}
    if default_category_id.strip():
        body["default_category_id"] = int(default_category_id)
    try:
        await request.app.state.clients.ledger.put(f"/api/payees/{payee_id}", json=body)
    except ServiceError as exc:
        return back("/payees", err=str(exc.detail))
    return back(f"/payees?show={payee_id}", msg="Payee updated")


@router.post("/payees/{payee_id}/aliases")
async def alias_add(
    request: Request, payee_id: int, pattern: str = Form(...), match_type: str = Form("exact")
):
    try:
        await request.app.state.clients.ledger.post(
            f"/api/payees/{payee_id}/aliases", json={"pattern": pattern, "match_type": match_type}
        )
    except ServiceError as exc:
        return back(f"/payees?show={payee_id}", err=str(exc.detail))
    return back(f"/payees?show={payee_id}", msg="Alias added")


@router.post("/payees/{payee_id}/aliases/{alias_id}/delete")
async def alias_delete(request: Request, payee_id: int, alias_id: int):
    await request.app.state.clients.ledger.delete(f"/api/payees/{payee_id}/aliases/{alias_id}")
    return back(f"/payees?show={payee_id}", msg="Alias removed")


@router.get("/categories")
async def categories_page(request: Request):
    clients = request.app.state.clients
    categories = await clients.ledger.get("/api/categories", params={"include_inactive": True})
    parents = [c for c in categories if c["parent_id"] is None]
    return render(request, "categories.html", {"categories": categories, "parents": parents})


@router.post("/categories")
async def category_create(
    request: Request, name: str = Form(...), kind: str = Form("expense"), parent_id: str = Form("")
):
    body = {"name": name, "kind": kind}
    if parent_id.strip():
        body["parent_id"] = int(parent_id)
    try:
        await request.app.state.clients.ledger.post("/api/categories", json=body)
    except ServiceError as exc:
        return back("/categories", err=str(exc.detail))
    return back("/categories", msg=f"Category '{name}' created")


@router.post("/categories/seed-defaults")
async def seed_defaults(request: Request):
    try:
        result = await request.app.state.clients.ledger.post("/api/seed-defaults")
    except ServiceError as exc:
        return back("/categories", err=str(exc.detail))
    return back("/categories", msg=f"Added {result['created']} starter categories")


@router.post("/categories/{category_id}/update")
async def category_update(request: Request, category_id: int, active: str = Form("")):
    try:
        await request.app.state.clients.ledger.put(
            f"/api/categories/{category_id}", json={"active": active == "on"}
        )
    except ServiceError as exc:
        return back("/categories", err=str(exc.detail))
    return back("/categories", msg="Category updated")
