from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..auth import PASSWORD_KEY, get_stored_hash, hash_password, verify_password
from ..clients import ServiceError
from ..rendering import render

router = APIRouter()


@router.get("/login")
async def login_page(request: Request):
    try:
        stored = await get_stored_hash(request)
    except ServiceError as exc:
        if exc.status == 404:
            stored = None
        else:
            return render(
                request, "login.html", {"first_run": False, "err": f"settings service: {exc.detail}"}
            )
    return render(request, "login.html", {"first_run": stored is None})


@router.post("/login")
async def login_submit(request: Request, password: str = Form("")):
    try:
        stored = await get_stored_hash(request)
    except ServiceError as exc:
        stored = None if exc.status == 404 else ""
    if not stored:
        return RedirectResponse("/login", status_code=303)
    if not verify_password(password, stored):
        return render(request, "login.html", {"first_run": False, "err": "Wrong password."})
    request.session["authenticated"] = True
    return RedirectResponse("/", status_code=303)


@router.post("/setup-password")
async def setup_password(request: Request, password: str = Form(""), password2: str = Form("")):
    try:
        stored = await get_stored_hash(request)
    except ServiceError as exc:
        stored = None if exc.status == 404 else ""
    if stored:
        # Password already exists; never allow an unauthenticated overwrite.
        return RedirectResponse("/login", status_code=303)
    if len(password) < 4:
        return render(
            request, "login.html", {"first_run": True, "err": "Use at least 4 characters."}
        )
    if password != password2:
        return render(
            request, "login.html", {"first_run": True, "err": "Passwords do not match."}
        )
    await request.app.state.clients.settings.put(
        f"/api/settings/{PASSWORD_KEY}",
        json={"value": hash_password(password), "is_secret": True},
    )
    request.session["authenticated"] = True
    return RedirectResponse("/?msg=Welcome+to+SakuraFinancial", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
