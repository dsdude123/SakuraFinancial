"""Bill scheduling and transaction matching. Fully implemented in the bills
phase; the matcher hook already exists so transaction creation can call it."""

from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import Transaction


def match_transaction_to_bills(db: Session, txn: Transaction) -> list[dict]:
    """Try to match a new transaction to open bill occurrences. Returns a list
    of match result dicts (empty until the bills phase lands)."""
    return []
