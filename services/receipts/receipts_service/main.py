"""receipts-service: receipt/invoice storage, parsing, and transaction linking.

Upload an Amazon invoice or a grocery receipt; the service stores the original
forever, extracts its text (pdfplumber / OCR / HTML stripping), optionally
parses it with the configured LLM into vendor/date/total + categorized line
items, and matches it against ledger transactions (exact amount, ±5 days).
Applying a receipt's split re-divides the one matched transaction across
categories via the ledger API.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from sakura_common import jsonutil
from sakura_common.money import money_str
from sakura_common.settings_client import SettingsClient

from .db import Base, get_db, get_ledger, get_settings_client, make_engine, make_session_factory
from .ledger_client import LedgerClient, LedgerUnavailable
from .logic import extraction, matching, parsing
from .models import Document, ReceiptItem


class DocumentUpdate(BaseModel):
    vendor: str | None = None
    doc_date: str | None = None
    total: str | None = None
    currency: str | None = None
    status: str | None = None


class ItemsIn(BaseModel):
    items: list[dict]


class LinkIn(BaseModel):
    transaction_id: int


def item_dict(item: ReceiptItem) -> dict:
    return {
        "id": item.id,
        "description": item.description,
        "amount": money_str(item.amount),
        "category_name": item.category_name,
        "category_id": item.category_id,
    }


def document_dict(document: Document, with_text: bool = False) -> dict:
    data = {
        "id": document.id,
        "filename": document.filename,
        "content_type": document.content_type,
        "source": document.source,
        "uploaded_at": document.uploaded_at.isoformat() if document.uploaded_at else None,
        "vendor": document.vendor,
        "doc_date": document.doc_date.isoformat() if document.doc_date else None,
        "total": money_str(document.total),
        "currency": document.currency,
        "status": document.status,
        "parse_note": document.parse_note,
        "linked_transaction_id": document.linked_transaction_id,
        "items": [item_dict(item) for item in document.items],
    }
    if with_text:
        data["extracted_text"] = document.extracted_text
    return data


def create_app(
    database_url: str | None = None,
    ledger_client: LedgerClient | None = None,
    settings_client=None,
    data_dir: str | None = None,
) -> FastAPI:
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)

    app = FastAPI(title="SakuraFinancial receipts-service", version="1.0")
    app.state.session_factory = make_session_factory(engine)
    app.state.ledger_client = ledger_client or LedgerClient()
    app.state.settings_client = settings_client or SettingsClient()
    app.state.data_dir = data_dir or os.environ.get("RECEIPTS_DATA_DIR", "./receipts-data")
    Path(app.state.data_dir).mkdir(parents=True, exist_ok=True)

    @app.exception_handler(LedgerUnavailable)
    def ledger_unavailable(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/documents")
    async def upload(
        file: UploadFile = File(...),
        db: Session = Depends(get_db),
        ledger=Depends(get_ledger),
        settings=Depends(get_settings_client),
    ):
        content = await file.read()
        if not content:
            raise HTTPException(422, "empty file")
        stored_name = f"{uuid.uuid4().hex}-{Path(file.filename or 'receipt').name}"
        (Path(app.state.data_dir) / stored_name).write_bytes(content)

        extracted, note = extraction.extract_text(
            content, file.content_type or "", file.filename or ""
        )
        document = Document(
            filename=file.filename or "receipt",
            content_type=file.content_type or "application/octet-stream",
            stored_name=stored_name,
            extracted_text=extracted,
            parse_note=note,
            status="new",
        )
        db.add(document)
        db.flush()

        try:
            categories = ledger.categories()
        except LedgerUnavailable:
            categories = []
        image_data = None
        if (file.content_type or "").startswith("image/"):
            image_data = (file.content_type, content)
        parsing.parse_document(db, document, settings, categories, image_data=image_data)
        if document.status == "parsed":
            # A fresh parse may already have exactly one matching transaction.
            matching.scan_unlinked(db, ledger)
        db.commit()
        return document_dict(document, with_text=True)

    @app.get("/api/documents")
    def list_documents(
        unlinked: bool = False,
        status: str | None = Query(None),
        db: Session = Depends(get_db),
    ):
        query = select(Document).order_by(Document.uploaded_at.desc())
        if unlinked:
            query = query.where(
                Document.linked_transaction_id.is_(None), Document.status != "ignored"
            )
        if status is not None:
            query = query.where(Document.status == status)
        return [document_dict(d) for d in db.execute(query).scalars()]

    @app.get("/api/documents/{document_id}")
    def get_document(document_id: int, db: Session = Depends(get_db)):
        document = db.get(Document, document_id)
        if document is None:
            raise HTTPException(404, f"no document {document_id}")
        return document_dict(document, with_text=True)

    @app.get("/api/documents/{document_id}/file")
    def get_file(document_id: int, db: Session = Depends(get_db)):
        document = db.get(Document, document_id)
        if document is None:
            raise HTTPException(404, f"no document {document_id}")
        path = Path(app.state.data_dir) / document.stored_name
        if not path.exists():
            raise HTTPException(404, "stored file missing from the receipts volume")
        return Response(path.read_bytes(), media_type=document.content_type)

    @app.put("/api/documents/{document_id}")
    def update_document(document_id: int, body: DocumentUpdate, db: Session = Depends(get_db)):
        document = db.get(Document, document_id)
        if document is None:
            raise HTTPException(404, f"no document {document_id}")
        if body.vendor is not None:
            document.vendor = body.vendor
        if body.doc_date is not None:
            document.doc_date = jsonutil.parse_date(body.doc_date) if body.doc_date else None
        if body.total is not None:
            try:
                document.total = Decimal(body.total) if body.total else None
            except InvalidOperation:
                raise HTTPException(422, f"bad total {body.total!r}")
        if body.currency is not None:
            document.currency = body.currency.upper()[:3]
        if body.status is not None:
            if body.status not in ("new", "parsed", "needs_review", "linked", "ignored"):
                raise HTTPException(422, f"bad status {body.status!r}")
            document.status = body.status
        db.commit()
        return document_dict(document, with_text=True)

    @app.put("/api/documents/{document_id}/items")
    def set_items(document_id: int, body: ItemsIn, db: Session = Depends(get_db)):
        document = db.get(Document, document_id)
        if document is None:
            raise HTTPException(404, f"no document {document_id}")
        document.items.clear()
        for item in body.items:
            try:
                amount = Decimal(str(item["amount"]))
            except (KeyError, InvalidOperation):
                raise HTTPException(422, f"bad item {item!r}")
            document.items.append(
                ReceiptItem(
                    description=str(item.get("description", ""))[:500],
                    amount=amount,
                    category_name=str(item.get("category_name", "")),
                    category_id=item.get("category_id"),
                )
            )
        db.commit()
        return document_dict(document)

    @app.post("/api/documents/{document_id}/parse")
    def reparse(
        document_id: int,
        db: Session = Depends(get_db),
        ledger=Depends(get_ledger),
        settings=Depends(get_settings_client),
    ):
        document = db.get(Document, document_id)
        if document is None:
            raise HTTPException(404, f"no document {document_id}")
        try:
            categories = ledger.categories()
        except LedgerUnavailable:
            categories = []
        image_data = None
        if document.content_type.startswith("image/"):
            path = Path(app.state.data_dir) / document.stored_name
            if path.exists():
                image_data = (document.content_type, path.read_bytes())
        parsing.parse_document(db, document, settings, categories, image_data=image_data)
        db.commit()
        return document_dict(document, with_text=True)

    @app.get("/api/documents/{document_id}/candidates")
    def get_candidates(document_id: int, db: Session = Depends(get_db), ledger=Depends(get_ledger)):
        document = db.get(Document, document_id)
        if document is None:
            raise HTTPException(404, f"no document {document_id}")
        return matching.candidates(document, ledger)

    @app.post("/api/documents/{document_id}/link")
    def link(document_id: int, body: LinkIn, db: Session = Depends(get_db), ledger=Depends(get_ledger)):
        document = db.get(Document, document_id)
        if document is None:
            raise HTTPException(404, f"no document {document_id}")
        ledger.get_transaction(body.transaction_id)  # 503/404 if bogus
        document.linked_transaction_id = body.transaction_id
        document.status = "linked"
        db.commit()
        return document_dict(document)

    @app.post("/api/documents/{document_id}/unlink")
    def unlink(document_id: int, db: Session = Depends(get_db)):
        document = db.get(Document, document_id)
        if document is None:
            raise HTTPException(404, f"no document {document_id}")
        document.linked_transaction_id = None
        document.status = "parsed" if (document.total or document.items) else "needs_review"
        db.commit()
        return document_dict(document)

    @app.post("/api/documents/{document_id}/apply-split")
    def apply_split(document_id: int, db: Session = Depends(get_db), ledger=Depends(get_ledger)):
        document = db.get(Document, document_id)
        if document is None:
            raise HTTPException(404, f"no document {document_id}")
        try:
            result = matching.apply_split(document, ledger)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        db.commit()
        return result

    @app.post("/api/match/scan")
    def match_scan(db: Session = Depends(get_db), ledger=Depends(get_ledger)):
        """Auto-link unambiguous receipts. The ledger calls this after every
        committed CSV import; the UI has a button too."""
        result = matching.scan_unlinked(db, ledger)
        db.commit()
        return result

    @app.get("/api/export")
    def export(db: Session = Depends(get_db)):
        documents = db.execute(select(Document).order_by(Document.id)).scalars().all()
        return {
            "service": "receipts",
            "note": "original files are exported separately at /api/export/files.zip",
            "documents": [
                {
                    "id": d.id,
                    "filename": d.filename,
                    "content_type": d.content_type,
                    "stored_name": d.stored_name,
                    "source": d.source,
                    "uploaded_at": d.uploaded_at.isoformat() if d.uploaded_at else None,
                    "vendor": d.vendor,
                    "doc_date": d.doc_date.isoformat() if d.doc_date else None,
                    "total": money_str(d.total),
                    "currency": d.currency,
                    "status": d.status,
                    "extracted_text": d.extracted_text,
                    "parse_note": d.parse_note,
                    "linked_transaction_id": d.linked_transaction_id,
                    "items": [
                        {
                            "description": item.description,
                            "amount": money_str(item.amount),
                            "category_name": item.category_name,
                            "category_id": item.category_id,
                        }
                        for item in d.items
                    ],
                }
                for d in documents
            ],
        }

    @app.get("/api/export/files.zip")
    def export_files(db: Session = Depends(get_db)):
        documents = db.execute(select(Document)).scalars().all()
        buffer = BytesIO()
        with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
            for document in documents:
                path = Path(app.state.data_dir) / document.stored_name
                if path.exists():
                    archive.write(path, arcname=document.stored_name)
        return Response(
            buffer.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": "attachment; filename=receipt-files.zip"},
        )

    @app.post("/api/reset")
    def reset(db: Session = Depends(get_db)):
        """Erase every record and come back up as a fresh install.

        Unlike the other services, receipts also owns files on disk: the
        original scans are deleted too, or a reset would leave the volume
        full of documents nothing references."""
        stored = [
            row.stored_name
            for row in db.execute(select(Document)).scalars()
            if row.stored_name
        ]
        deleted = {
            "receipt_items": db.execute(text("DELETE FROM receipt_items")).rowcount,
            "documents": db.execute(text("DELETE FROM documents")).rowcount,
        }
        removed = 0
        for name in stored:
            path = Path(app.state.data_dir) / Path(name).name  # no path traversal
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass  # already gone, or never written — nothing to recover
        db.commit()
        return {"reset": "receipts", "deleted": deleted, "files_removed": removed}

    @app.post("/api/import")
    def import_(data: dict, db: Session = Depends(get_db)):
        if not isinstance(data.get("documents"), list):
            raise HTTPException(422, "expected {'documents': [...]} export format")
        db.execute(text("DELETE FROM receipt_items"))
        db.execute(text("DELETE FROM documents"))
        for d in data["documents"]:
            document = Document(
                id=d["id"],
                filename=d["filename"],
                content_type=d.get("content_type", "application/octet-stream"),
                stored_name=d["stored_name"],
                source=d.get("source", "upload"),
                uploaded_at=jsonutil.parse_datetime(d.get("uploaded_at")),
                vendor=d.get("vendor", ""),
                doc_date=jsonutil.parse_date(d.get("doc_date")),
                total=jsonutil.parse_decimal(d.get("total")),
                currency=d.get("currency", "USD"),
                status=d.get("status", "new"),
                extracted_text=d.get("extracted_text", ""),
                parse_note=d.get("parse_note", ""),
                linked_transaction_id=d.get("linked_transaction_id"),
            )
            for item in d.get("items", []):
                document.items.append(
                    ReceiptItem(
                        description=item.get("description", ""),
                        amount=jsonutil.parse_decimal(item["amount"]),
                        category_name=item.get("category_name", ""),
                        category_id=item.get("category_id"),
                    )
                )
            db.add(document)
        if db.get_bind().dialect.name == "postgresql":
            for table in ("documents", "receipt_items"):
                db.execute(
                    text(
                        f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                        f"COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)"
                    )
                )
        db.commit()
        return {"imported": len(data["documents"])}

    @app.post("/api/import/files")
    async def import_files(file: UploadFile = File(...)):
        content = await file.read()
        restored = 0
        with ZipFile(BytesIO(content)) as archive:
            for name in archive.namelist():
                safe = Path(name).name  # no path traversal
                (Path(app.state.data_dir) / safe).write_bytes(archive.read(name))
                restored += 1
        return {"restored_files": restored}

    return app
