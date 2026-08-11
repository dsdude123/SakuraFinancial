"""Matching receipts to ledger transactions and applying category splits.

Match rule from the requirements: **exact charge amount**, date within ±5
days, vendor similarity as a ranking signal. When a scan finds exactly one
candidate the link is automatic; ambiguity is left for the user. The ledger
pings ``/api/match/scan`` after each CSV import so freshly imported Amazon
charges pick up their waiting invoices.

Applying a split re-divides the ONE matched transaction's total across the
receipt's item categories via the ledger API — never new transactions, and
the ledger enforces that split sums equal the transaction total (a filler
line balances rounding/tips).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from sakura_common.money import money_str

from ..models import Document

DATE_WINDOW_DAYS = 5


def _tokens(text: str) -> set[str]:
    return {token for token in text.upper().replace("*", " ").split() if len(token) >= 3}


def candidates(document: Document, ledger) -> list[dict]:
    """Unmatched-side search: ledger transactions with the receipt's exact
    amount in the date window, ranked by vendor-name overlap."""
    if document.total is None or document.doc_date is None:
        return []
    start = document.doc_date - timedelta(days=DATE_WINDOW_DAYS)
    end = document.doc_date + timedelta(days=DATE_WINDOW_DAYS)
    rows = ledger.search_transactions(amount=document.total, start=start, end=end)
    vendor_tokens = _tokens(document.vendor)

    def rank(txn: dict) -> tuple:
        haystack = _tokens(f"{txn.get('payee_name') or ''} {txn.get('memo') or ''}")
        overlap = len(vendor_tokens & haystack)
        distance = abs(
            (document.doc_date - _parse_date(txn["date"])).days
        )
        return (-overlap, distance)

    rows = [txn for txn in rows if txn.get("kind") == "normal"]
    rows.sort(key=rank)
    return rows


def _parse_date(value: str):
    from datetime import date

    return date.fromisoformat(value)


def scan_unlinked(db: Session, ledger) -> dict:
    """Auto-link every parsed, unlinked document that has exactly one
    candidate. Called on demand and after ledger imports."""
    documents = db.execute(
        select(Document).where(
            Document.status.in_(("parsed", "needs_review")),
            Document.linked_transaction_id.is_(None),
        )
    ).scalars().all()
    linked = 0
    ambiguous = 0
    for document in documents:
        found = candidates(document, ledger)
        if len(found) == 1:
            document.linked_transaction_id = found[0]["id"]
            document.status = "linked"
            linked += 1
        elif len(found) > 1:
            ambiguous += 1
    return {"scanned": len(documents), "linked": linked, "ambiguous": ambiguous}


def apply_split(document: Document, ledger) -> dict:
    """Push the receipt's items as the matched transaction's category splits."""
    if document.linked_transaction_id is None:
        raise ValueError("document is not linked to a transaction")
    if not document.items:
        raise ValueError("document has no line items to apply")
    txn = ledger.get_transaction(document.linked_transaction_id)
    total = Decimal(txn["total"])
    # Receipt items are positive; an expense transaction is negative.
    sign = Decimal("-1") if total < 0 else Decimal("1")
    splits = []
    allocated = Decimal("0")
    for item in document.items:
        amount = sign * abs(item.amount)
        allocated += amount
        splits.append(
            {
                "category_id": item.category_id,
                "amount": str(amount),
                "memo": item.description[:200],
            }
        )
    remainder = total - allocated
    if remainder != 0:
        # Rounding, tips, or unparsed lines: a balancing line keeps the
        # invariant (split sum == transaction total) honest and visible.
        splits.append(
            {"category_id": None, "amount": str(remainder), "memo": "(receipt rounding)"}
        )
    updated = ledger.replace_splits(document.linked_transaction_id, splits)
    return {
        "transaction_id": document.linked_transaction_id,
        "splits_applied": len(splits),
        "remainder": money_str(remainder),
        "transaction": updated,
    }
