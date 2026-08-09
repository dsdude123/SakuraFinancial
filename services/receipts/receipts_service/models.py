"""Receipts data model.

A Document is an uploaded receipt/invoice (Amazon order PDF, grocery receipt
photo, HTML invoice). Originals are stored forever on the receipts volume;
the database holds extracted text, the parsed guesses (vendor/date/total),
per-line items with category guesses, and — once matched — a link to the ONE
ledger transaction it documents. Applying a receipt's split re-divides that
transaction's total across categories via the ledger API; it never creates
transactions.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

DOCUMENT_STATUSES = ("new", "parsed", "needs_review", "linked", "ignored")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(300))
    content_type: Mapped[str] = mapped_column(String(100), default="application/octet-stream")
    stored_name: Mapped[str] = mapped_column(String(300))  # file on the receipts volume
    source: Mapped[str] = mapped_column(String(10), default="upload")
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    vendor: Mapped[str] = mapped_column(String(200), default="")
    doc_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    total: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    status: Mapped[str] = mapped_column(String(15), default="new", index=True)
    extracted_text: Mapped[str] = mapped_column(Text, default="")
    parse_note: Mapped[str] = mapped_column(Text, default="")
    linked_transaction_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    items: Mapped[list["ReceiptItem"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", order_by="ReceiptItem.id"
    )


class ReceiptItem(Base):
    """One line of the receipt with a category guess — the raw material for a
    multi-category split of the matched transaction."""

    __tablename__ = "receipt_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    description: Mapped[str] = mapped_column(Text, default="")
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    category_name: Mapped[str] = mapped_column(String(150), default="")
    category_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    document: Mapped[Document] = relationship(back_populates="items")
