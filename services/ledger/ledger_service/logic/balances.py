"""Account balance computation: opening balance + sum of splits."""

from __future__ import annotations

from datetime import date as date_type
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Account, Split, Transaction


def account_balance(db: Session, account: Account, as_of: date_type | None = None) -> Decimal:
    query = (
        select(func.coalesce(func.sum(Split.amount), 0))
        .join(Transaction, Split.transaction_id == Transaction.id)
        .where(Transaction.account_id == account.id)
    )
    if as_of is not None:
        query = query.where(Transaction.date <= as_of)
    movement = db.execute(query).scalar_one()
    return account.opening_balance + Decimal(str(movement))
