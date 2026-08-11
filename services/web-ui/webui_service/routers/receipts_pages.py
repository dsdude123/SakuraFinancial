from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response

from ..auth import require_login
from ..clients import ServiceError
from ..rendering import render
from .accounts import back

router = APIRouter(dependencies=[Depends(require_login)])


@router.get("/receipts")
async def receipts_inbox(request: Request):
    clients = request.app.state.clients
    unlinked = await clients.receipts.get("/api/documents", params={"unlinked": True})
    linked = await clients.receipts.get("/api/documents", params={"status": "linked"})
    return render(
        request,
        "receipts.html",
        {"unlinked": unlinked, "linked": linked[:15]},
    )


@router.post("/receipts/upload")
async def receipts_upload(request: Request, file: UploadFile = File(...)):
    raw = await file.read()
    try:
        document = await request.app.state.clients.receipts.request(
            "POST",
            "/api/documents",
            files={"file": (file.filename or "receipt", raw, file.content_type or "application/octet-stream")},
        )
    except ServiceError as exc:
        return back("/receipts", err=str(exc.detail))
    if document["status"] == "linked":
        return back(f"/receipts/{document['id']}", msg="Uploaded and matched automatically")
    return back(f"/receipts/{document['id']}", msg="Uploaded — review below")


@router.post("/receipts/scan")
async def receipts_scan(request: Request):
    try:
        result = await request.app.state.clients.receipts.post("/api/match/scan")
    except ServiceError as exc:
        return back("/receipts", err=str(exc.detail))
    return back(
        "/receipts",
        msg=f"Scanned {result['scanned']}: linked {result['linked']}, "
        f"{result['ambiguous']} ambiguous",
    )


@router.get("/receipts/{document_id}")
async def receipt_detail(request: Request, document_id: int, rows: int = 0):
    clients = request.app.state.clients
    document = await clients.receipts.get(f"/api/documents/{document_id}")
    categories = await clients.ledger.get("/api/categories")
    candidates = []
    linked_txn = None
    try:
        if document["linked_transaction_id"]:
            linked_txn = await clients.ledger.get(
                f"/api/transactions/{document['linked_transaction_id']}"
            )
        else:
            candidates = await clients.receipts.get(
                f"/api/documents/{document_id}/candidates"
            )
    except ServiceError:
        pass
    item_rows = max(len(document["items"]), rows, 1)
    return render(
        request,
        "receipt_detail.html",
        {
            "doc": document,
            "categories": categories,
            "candidates": candidates,
            "linked_txn": linked_txn,
            "item_rows": item_rows,
        },
    )


@router.get("/receipts/{document_id}/file")
async def receipt_file(request: Request, document_id: int):
    document = await request.app.state.clients.receipts.get(f"/api/documents/{document_id}")
    content = await request.app.state.clients.receipts.get(f"/api/documents/{document_id}/file")
    return Response(content, media_type=document["content_type"])


@router.post("/receipts/{document_id}/update")
async def receipt_update(
    request: Request,
    document_id: int,
    vendor: str = Form(""),
    doc_date: str = Form(""),
    total: str = Form(""),
):
    try:
        await request.app.state.clients.receipts.put(
            f"/api/documents/{document_id}",
            json={"vendor": vendor, "doc_date": doc_date, "total": total},
        )
    except ServiceError as exc:
        return back(f"/receipts/{document_id}", err=str(exc.detail))
    return back(f"/receipts/{document_id}", msg="Saved")


@router.post("/receipts/{document_id}/items")
async def receipt_items(request: Request, document_id: int):
    form = await request.form()
    items = []
    index = 0
    while f"item_amount_{index}" in form:
        amount = str(form.get(f"item_amount_{index}", "")).strip()
        if amount:
            category = str(form.get(f"item_category_{index}", "")).strip()
            items.append(
                {
                    "description": str(form.get(f"item_desc_{index}", "")),
                    "amount": amount,
                    "category_id": int(category) if category else None,
                }
            )
        index += 1
    if "action_add" in form:
        return back(f"/receipts/{document_id}?rows={index + 1}")
    try:
        await request.app.state.clients.receipts.put(
            f"/api/documents/{document_id}/items", json={"items": items}
        )
    except ServiceError as exc:
        return back(f"/receipts/{document_id}", err=str(exc.detail))
    return back(f"/receipts/{document_id}", msg="Items saved")


@router.post("/receipts/{document_id}/reparse")
async def receipt_reparse(request: Request, document_id: int):
    try:
        await request.app.state.clients.receipts.post(f"/api/documents/{document_id}/parse")
    except ServiceError as exc:
        return back(f"/receipts/{document_id}", err=str(exc.detail))
    return back(f"/receipts/{document_id}", msg="Re-parsed with AI")


@router.post("/receipts/{document_id}/link")
async def receipt_link(request: Request, document_id: int, transaction_id: int = Form(...)):
    try:
        await request.app.state.clients.receipts.post(
            f"/api/documents/{document_id}/link", json={"transaction_id": transaction_id}
        )
    except ServiceError as exc:
        return back(f"/receipts/{document_id}", err=str(exc.detail))
    return back(f"/receipts/{document_id}", msg="Linked")


@router.post("/receipts/{document_id}/unlink")
async def receipt_unlink(request: Request, document_id: int):
    await request.app.state.clients.receipts.post(f"/api/documents/{document_id}/unlink")
    return back(f"/receipts/{document_id}", msg="Unlinked")


@router.post("/receipts/{document_id}/ignore")
async def receipt_ignore(request: Request, document_id: int):
    await request.app.state.clients.receipts.put(
        f"/api/documents/{document_id}", json={"status": "ignored"}
    )
    return back("/receipts", msg="Receipt ignored (kept on file)")


@router.post("/receipts/{document_id}/apply-split")
async def receipt_apply_split(request: Request, document_id: int):
    try:
        result = await request.app.state.clients.receipts.post(
            f"/api/documents/{document_id}/apply-split"
        )
    except ServiceError as exc:
        return back(f"/receipts/{document_id}", err=str(exc.detail))
    return back(
        f"/receipts/{document_id}",
        msg=f"Split applied: transaction {result['transaction_id']} now has "
        f"{result['splits_applied']} category line(s)",
    )
