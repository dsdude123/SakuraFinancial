"""The technical-details pages behind an error report."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..auth import require_login
from ..rendering import render

router = APIRouter(dependencies=[Depends(require_login)])


@router.get("/errors")
async def error_list(request: Request):
    return render(request, "errors.html", {"entries": request.app.state.errors.recent()})


@router.get("/errors/{error_id}")
async def error_detail(request: Request, error_id: str):
    entry = request.app.state.errors.get(error_id)
    return render(
        request,
        "error_detail.html",
        {"entry": entry, "error_id": error_id},
    )
