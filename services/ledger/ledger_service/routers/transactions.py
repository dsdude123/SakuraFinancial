from __future__ import annotations

import datetime as dt
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from ..db import get_db
from ..logic.transactions import (
    create_transaction,
    create_transfer,
    replace_splits,
)
from ..models import (
    Account,
    Category,
    Payee,
    Split,
    TRANSACTION_STATUSES,
    Transaction,
)
from ..serialize import transaction_dict

router = APIRouter(prefix="/api", tags=["transactions"])


class SplitIn(BaseModel):
    category_id: int | None = None
    amount: Decimal
    memo: str = ""


class TransactionIn(BaseModel):
    account_id: int
    date: dt.date
    payee_id: int | None = None
    memo: str = ""
    status: str = "uncleared"
    splits: list[SplitIn]


class TransactionUpdate(BaseModel):
    date: dt.date | None = None
    payee_id: int | None = None
    memo: str | None = None
    status: str | None = None
    splits: list[SplitIn] | None = None


class SplitsReplaceIn(BaseModel):
    splits: list[SplitIn]


class TransferIn(BaseModel):
    from_account_id: int
    to_account_id: int
    date: dt.date
    amount: Decimal
    to_amount: Decimal | None = None
    memo: str = ""


def load_txn_or_404(db: Session, transaction_id: int) -> Transaction:
    txn = db.execute(
        select(Transaction)
        .options(joinedload(Transaction.splits), joinedload(Transaction.payee), joinedload(Transaction.account))
        .where(Transaction.id == transaction_id)
    ).unique().scalar_one_or_none()
    if txn is None:
        raise HTTPException(404, f"no transaction {transaction_id}")
    return txn


def validate_refs(db: Session, payee_id: int | None, splits: list[SplitIn] | None):
    if payee_id is not None and db.get(Payee, payee_id) is None:
        raise HTTPException(422, f"no payee {payee_id}")
    for split in splits or []:
        if split.category_id is not None and db.get(Category, split.category_id) is None:
            raise HTTPException(422, f"no category {split.category_id}")


@router.get("/transactions")
def list_transactions(
    account_id: int | None = None,
    start: dt.date | None = None,
    end: dt.date | None = None,
    category_id: int | None = None,
    payee_id: int | None = None,
    kind: str | None = None,
    q: str | None = None,
    amount: Decimal | None = Query(None, description="match |total| exactly"),
    uncategorized: bool = False,
    limit: int = Query(100, le=1000),
    offset: int = 0,
    db: Session = Depends(get_db),
):
    query = (
        select(Transaction)
        .options(joinedload(Transaction.splits), joinedload(Transaction.payee), joinedload(Transaction.account))
        .order_by(Transaction.date.desc(), Transaction.id.desc())
    )
    if account_id is not None:
        query = query.where(Transaction.account_id == account_id)
    if start is not None:
        query = query.where(Transaction.date >= start)
    if end is not None:
        query = query.where(Transaction.date <= end)
    if kind is not None:
        query = query.where(Transaction.kind == kind)
    if payee_id is not None:
        query = query.where(Transaction.payee_id == payee_id)
    if category_id is not None:
        query = query.where(
            Transaction.id.in_(select(Split.transaction_id).where(Split.category_id == category_id))
        )
    if uncategorized:
        query = query.where(
            Transaction.kind == "normal",
            Transaction.id.in_(select(Split.transaction_id).where(Split.category_id.is_(None))),
        )
    if q:
        needle = f"%{q}%"
        query = query.outerjoin(Payee, Transaction.payee_id == Payee.id).where(
            or_(Payee.name.ilike(needle), Transaction.memo.ilike(needle))
        )
    if amount is not None:
        totals = (
            select(Split.transaction_id)
            .group_by(Split.transaction_id)
            .having(or_(func.sum(Split.amount) == amount, func.sum(Split.amount) == -amount))
        )
        query = query.where(Transaction.id.in_(totals))
    rows = db.execute(query.limit(limit).offset(offset)).unique().scalars().all()
    return [transaction_dict(t) for t in rows]


@router.post("/transactions")
def create(body: TransactionIn, db: Session = Depends(get_db)):
    account = db.get(Account, body.account_id)
    if account is None:
        raise HTTPException(422, f"no account {body.account_id}")
    if body.status not in TRANSACTION_STATUSES:
        raise HTTPException(422, f"status must be one of {TRANSACTION_STATUSES}")
    validate_refs(db, body.payee_id, body.splits)
    txn = create_transaction(
        db,
        account=account,
        date=body.date,
        payee_id=body.payee_id,
        memo=body.memo,
        status=body.status,
        splits=[split.model_dump() for split in body.splits],
    )
    db.commit()

    from ..logic.bills import match_transaction_to_bills  # imported late: phase 5 wiring

    matches = match_transaction_to_bills(db, txn)
    db.commit()
    result = transaction_dict(txn)
    result["bill_matches"] = matches
    return result


@router.get("/transactions/{transaction_id}")
def get_one(transaction_id: int, db: Session = Depends(get_db)):
    return transaction_dict(load_txn_or_404(db, transaction_id))


@router.put("/transactions/{transaction_id}")
def update(transaction_id: int, body: TransactionUpdate, db: Session = Depends(get_db)):
    txn = load_txn_or_404(db, transaction_id)
    validate_refs(db, body.payee_id, body.splits)
    if body.date is not None:
        txn.date = body.date
    if body.payee_id is not None:
        if txn.kind != "normal":
            raise HTTPException(422, f"a {txn.kind} transaction has no payee")
        txn.payee_id = body.payee_id
    if body.memo is not None:
        txn.memo = body.memo
    if body.status is not None:
        if body.status not in TRANSACTION_STATUSES:
            raise HTTPException(422, f"status must be one of {TRANSACTION_STATUSES}")
        txn.status = body.status
    if body.splits is not None:
        replace_splits(txn, [split.model_dump() for split in body.splits])
    db.commit()
    return transaction_dict(txn)


@router.put("/transactions/{transaction_id}/splits")
def set_splits(transaction_id: int, body: SplitsReplaceIn, db: Session = Depends(get_db)):
    """Re-divide one transaction's total across categories. Used by manual
    split editing and by receipts applying a parsed split. The sum must equal
    the existing total — a split never changes what the account saw."""
    txn = load_txn_or_404(db, transaction_id)
    validate_refs(db, None, body.splits)
    replace_splits(txn, [split.model_dump() for split in body.splits])
    db.commit()
    return transaction_dict(txn)


@router.delete("/transactions/{transaction_id}")
def delete(transaction_id: int, db: Session = Depends(get_db)):
    txn = load_txn_or_404(db, transaction_id)
    deleted = [txn.id]
    if txn.transfer_group_id:
        # Transfers are atomic pairs; removing one leg removes both.
        for leg in db.execute(
            select(Transaction).where(Transaction.transfer_group_id == txn.transfer_group_id)
        ).scalars():
            if leg.id != txn.id:
                deleted.append(leg.id)
                db.delete(leg)
    db.delete(txn)
    db.commit()
    return {"deleted": deleted}


@router.post("/transfers")
def transfer(body: TransferIn, db: Session = Depends(get_db)):
    from_account = db.get(Account, body.from_account_id)
    to_account = db.get(Account, body.to_account_id)
    if from_account is None or to_account is None:
        raise HTTPException(422, "unknown account")
    if (
        from_account.currency_code != to_account.currency_code
        and body.to_amount is None
    ):
        raise HTTPException(
            422,
            f"accounts use different currencies ({from_account.currency_code} -> "
            f"{to_account.currency_code}); provide to_amount",
        )
    leg_out, leg_in = create_transfer(
        db,
        from_account=from_account,
        to_account=to_account,
        date=body.date,
        amount=body.amount,
        to_amount=body.to_amount,
        memo=body.memo,
    )
    db.commit()
    return {"out": transaction_dict(leg_out), "in": transaction_dict(leg_in)}
