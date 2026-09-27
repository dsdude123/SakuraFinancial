from __future__ import annotations

import datetime as dt
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy import update as sql_update
from sqlalchemy.orm import Session, joinedload

from ..db import get_db
from ..logic.transactions import (
    create_transaction,
    create_external_transfer,
    create_transfer,
    replace_splits,
)
from ..models import (
    Account,
    Category,
    ImportRow,
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


class ExternalTransferIn(BaseModel):
    """A transfer between one ledger account and an account owned by another
    service (a brokerage in the stocks service). ``direction`` is from this
    account's point of view; ``amount`` is always positive."""

    account_id: int
    date: dt.date
    amount: Decimal
    direction: str = "out"
    external_account: str
    external_name: str = ""
    memo: str = ""
    transfer_group_id: str | None = None


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

    def release_import_rows(ids: list[int]) -> None:
        """Let go of the review rows that produced these transactions.

        ``import_rows.transaction_id`` is a real foreign key, so deleting an
        imported transaction without clearing it fails outright on Postgres.
        Clearing it also leaves the truth: that row's money is no longer in the
        register, so re-importing the statement offers it again instead of
        calling it a duplicate.
        """
        db.execute(
            sql_update(ImportRow)
            .where(ImportRow.transaction_id.in_(ids))
            .values(transaction_id=None)
        )
    if txn.transfer_group_id:
        # Transfers are atomic pairs; removing one leg removes both.
        for leg in db.execute(
            select(Transaction).where(Transaction.transfer_group_id == txn.transfer_group_id)
        ).scalars():
            if leg.id != txn.id:
                deleted.append(leg.id)
                db.delete(leg)
    external_account, group = txn.external_account, txn.transfer_group_id
    release_import_rows(deleted)
    db.delete(txn)
    db.commit()
    # A leg whose other side lives in another service can't be deleted from
    # here, so say so instead of silently leaving half a transfer behind.
    return {
        "deleted": deleted,
        "transfer_group_id": group,
        "external_account": external_account,
    }


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


@router.post("/transfers/external")
def external_transfer(body: ExternalTransferIn, db: Session = Depends(get_db)):
    """Book this side of a transfer to or from an account another service owns.

    The caller (the web UI) writes the other side and passes the group id so
    both rows agree, or takes the generated one from the response.
    """
    account = db.get(Account, body.account_id)
    if account is None:
        raise HTTPException(422, "unknown account")
    leg = create_external_transfer(
        db,
        account=account,
        date=body.date,
        amount=body.amount,
        direction=body.direction,
        external_account=body.external_account,
        external_name=body.external_name,
        memo=body.memo,
        transfer_group_id=body.transfer_group_id,
    )
    db.commit()
    return transaction_dict(leg)


class BulkEdit(BaseModel):
    """Apply one change to many transactions at once. Every field is optional;
    only the ones given are touched."""

    transaction_ids: list[int]
    category_id: int | None = None
    payee_id: int | None = None
    status: str | None = None


@router.post("/transactions/bulk")
def bulk_edit(body: BulkEdit, db: Session = Depends(get_db)):
    """Recategorize (or re-payee, or mark cleared) a set of transactions.

    Filing a year of imported rows one at a time is the job this avoids. Two
    deliberate refusals, reported rather than guessed at:

    * A **split** transaction is skipped when changing the category. Collapsing
      several categories into one would silently destroy how the money was
      actually divided, and there is no way to guess which category was meant.
    * A **transfer or valuation** is skipped for category and payee. Those have
      neither by design; the register would start lying about what they are.
    """
    if not body.transaction_ids:
        raise HTTPException(422, "Select at least one transaction to change.")
    if body.status is not None and body.status not in TRANSACTION_STATUSES:
        raise HTTPException(422, f"Status must be one of {TRANSACTION_STATUSES}.")
    if body.category_id is not None and db.get(Category, body.category_id) is None:
        raise HTTPException(422, f"Category {body.category_id} no longer exists.")
    if body.payee_id is not None and db.get(Payee, body.payee_id) is None:
        raise HTTPException(422, f"Payee {body.payee_id} no longer exists.")
    if body.category_id is None and body.payee_id is None and body.status is None:
        raise HTTPException(422, "Choose a category, a payee or a status to apply.")

    transactions = (
        db.execute(
            select(Transaction)
            .options(joinedload(Transaction.splits))
            .where(Transaction.id.in_(body.transaction_ids))
        )
        .unique()
        .scalars()
        .all()
    )
    found = {txn.id for txn in transactions}
    updated = 0
    skipped: list[dict] = []
    for txn in transactions:
        reasons = []
        if body.category_id is not None or body.payee_id is not None:
            if txn.kind != "normal":
                reasons.append(f"it is a {txn.kind}, which has no payee or category")
        if body.category_id is not None and txn.kind == "normal" and len(txn.splits) > 1:
            reasons.append(f"it is split across {len(txn.splits)} categories")
        if reasons:
            skipped.append({"id": txn.id, "reason": reasons[0]})
            continue
        if body.category_id is not None:
            txn.splits[0].category_id = body.category_id
        if body.payee_id is not None:
            txn.payee_id = body.payee_id
        if body.status is not None:
            txn.status = body.status
        updated += 1
    for missing in [i for i in body.transaction_ids if i not in found]:
        skipped.append({"id": missing, "reason": "it no longer exists"})
    db.commit()
    return {"updated": updated, "skipped": skipped}
