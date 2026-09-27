from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request

from ..auth import PASSWORD_KEY, hash_password, require_login, verify_password
from ..clients import ServiceError
from ..rendering import render
from .accounts import back

router = APIRouter(dependencies=[Depends(require_login)])

LLM_KEYS = ("llm.provider", "llm.api_key", "llm.model", "llm.base_url")


@router.get("/settings")
async def settings_page(request: Request):
    clients = request.app.state.clients
    stored = {row["key"]: row["value"] for row in await clients.settings.get("/api/settings")}
    fx = await clients.ledger.get("/api/fx")
    currencies = await clients.ledger.get("/api/currencies")
    rules = await clients.ledger.get("/api/transfer-rules")
    accounts = await clients.ledger.get("/api/accounts")
    return render(
        request,
        "settings.html",
        {
            "stored": stored,
            "fx": fx,
            "currencies": currencies,
            "rules": rules,
            "accounts": accounts,
        },
    )


@router.post("/settings/llm")
async def save_llm(
    request: Request,
    provider: str = Form(""),
    api_key: str = Form(""),
    model: str = Form(""),
    base_url: str = Form(""),
):
    clients = request.app.state.clients
    values = {
        "llm.provider": (provider, False),
        "llm.api_key": (api_key, True),
        "llm.model": (model, False),
        "llm.base_url": (base_url, False),
    }
    for key, (value, secret) in values.items():
        if value.strip() or key in ("llm.base_url",):
            await clients.settings.put(
                f"/api/settings/{key}", json={"value": value.strip(), "is_secret": secret}
            )
    return back("/settings", msg="AI settings saved")


@router.post("/settings/llm/test")
async def test_llm(request: Request):
    try:
        result = await request.app.state.clients.settings.post("/api/llm/test", json={})
    except ServiceError as exc:
        return back("/settings", err=str(exc.detail))
    if result.get("ok"):
        return back("/settings", msg=f"AI connection works — model replied: {result['reply']}")
    return back("/settings", err=f"AI test failed: {result.get('error')}")


@router.post("/settings/ai-instructions")
async def save_ai_instructions(request: Request, instructions: str = Form("")):
    await request.app.state.clients.settings.put(
        "/api/settings/stocks.ai_instructions",
        json={"value": instructions, "is_secret": False},
    )
    return back("/settings", msg="Trading instructions saved")


@router.post("/settings/password")
async def change_password(
    request: Request,
    current: str = Form(""),
    password: str = Form(""),
    password2: str = Form(""),
):
    clients = request.app.state.clients
    stored = await clients.settings.get(
        f"/api/settings/{PASSWORD_KEY}", params={"reveal": "true"}
    )
    if not verify_password(current, stored.get("value", "")):
        return back("/settings", err="Current password is wrong")
    if len(password) < 4:
        return back("/settings", err="New password too short (4+ characters)")
    if password != password2:
        return back("/settings", err="New passwords don't match")
    await clients.settings.put(
        f"/api/settings/{PASSWORD_KEY}",
        json={"value": hash_password(password), "is_secret": True},
    )
    return back("/settings", msg="Password changed")


@router.post("/settings/currencies")
async def add_currency(
    request: Request, code: str = Form(...), name: str = Form(...), decimals: int = Form(2)
):
    try:
        await request.app.state.clients.ledger.post(
            "/api/currencies", json={"code": code, "name": name, "decimals": decimals}
        )
    except ServiceError as exc:
        return back("/settings", err=str(exc.detail))
    return back("/settings", msg=f"Currency {code.upper()} added")


@router.post("/settings/fx")
async def add_fx(
    request: Request,
    date: str = Form(...),
    from_code: str = Form(...),
    to_code: str = Form(...),
    rate: str = Form(...),
):
    try:
        await request.app.state.clients.ledger.post(
            "/api/fx",
            json={"date": date, "from_code": from_code, "to_code": to_code, "rate": rate},
        )
    except ServiceError as exc:
        return back("/settings", err=str(exc.detail))
    return back("/settings", msg="Exchange rate saved")


@router.post("/settings/fx/{fx_id}/delete")
async def delete_fx(request: Request, fx_id: int):
    await request.app.state.clients.ledger.delete(f"/api/fx/{fx_id}")
    return back("/settings", msg="Rate removed")


@router.post("/settings/transfer-rules")
async def add_rule(
    request: Request,
    match_days: str = Form("5"),
    pattern: str = Form(...),
    match_type: str = Form("prefix"),
    account_id: int = Form(...),
):
    try:
        await request.app.state.clients.ledger.post(
            "/api/transfer-rules",
            json={
                "pattern": pattern,
                "match_type": match_type,
                "account_id": account_id,
                "match_days": int(match_days) if str(match_days).strip() else 5,
            },
        )
    except ServiceError as exc:
        return back("/settings", err=str(exc.detail))
    return back("/settings", msg="Transfer rule added")


@router.post("/settings/transfer-rules/{rule_id}/delete")
async def delete_rule(request: Request, rule_id: int):
    await request.app.state.clients.ledger.delete(f"/api/transfer-rules/{rule_id}")
    return back("/settings", msg="Transfer rule removed")
