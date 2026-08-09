from __future__ import annotations

import datetime as dt
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from sakura_common.money import money_str

from ..db import get_db
from ..logic import bills as bill_logic
from ..models import (
    Account,
    BILL_FREQUENCIES,
    Bill,
    BillOccurrence,
    Category,
    Payee,
)
from ..serialize import category_path

router = APIRouter(prefix="/api/bills", tags=["bills"])


class BillIn(BaseModel):
    name: str
    payee_id: int
    category_id: int | None = None
    account_id: int | None = None
    frequency: str = "monthly"
    amount: Decimal
    is_variable: bool = False
    next_due: dt.date
    note: str = ""


class BillUpdate(BaseModel):
    name: str | None = None
    category_id: int | None = None
    account_id: int | None = None
    amount: Decimal | None = None
    is_variable: bool | None = None
    next_due: dt.date | None = None
    active: bool | None = None
    note: str | None = None


class ResolveIn(BaseModel):
    action: str  # "update_bill" | "keep"


def bill_dict(bill: Bill) -> dict:
    return {
        "id": bill.id,
        "name": bill.name,
        "payee_id": bill.payee_id,
        "payee_name": bill.payee.name if bill.payee else None,
        "category_id": bill.category_id,
        "category_name": category_path(bill.category),
        "account_id": bill.account_id,
        "frequency": bill.frequency,
        "amount": money_str(bill.amount),
        "is_variable": bill.is_variable,
        "next_due": bill.next_due.isoformat(),
        "active": bill.active,
        "note": bill.note,
        "monthly_load": money_str(bill_logic.monthly_load(bill)),
    }


def validate_bill_refs(db: Session, payee_id=None, category_id=None, account_id=None):
    if payee_id is not None and db.get(Payee, payee_id) is None:
        raise HTTPException(422, f"no payee {payee_id}")
    if category_id is not None and db.get(Category, category_id) is None:
        raise HTTPException(422, f"no category {category_id}")
    if account_id is not None and db.get(Account, account_id) is None:
        raise HTTPException(422, f"no account {account_id}")


@router.get("")
def list_bills(include_inactive: bool = False, db: Session = Depends(get_db)):
    query = select(Bill).order_by(Bill.name)
    if not include_inactive:
        query = query.where(Bill.active)
    return [bill_dict(b) for b in db.execute(query).scalars().all()]


@router.post("")
def create_bill(body: BillIn, db: Session = Depends(get_db)):
    if body.frequency not in BILL_FREQUENCIES:
        raise HTTPException(422, f"frequency must be one of {BILL_FREQUENCIES}")
    if body.amount <= 0:
        raise HTTPException(422, "amount must be positive (the expected outflow)")
    validate_bill_refs(db, body.payee_id, body.category_id, body.account_id)
    bill = Bill(
        name=body.name,
        payee_id=body.payee_id,
        category_id=body.category_id,
        account_id=body.account_id,
        frequency=body.frequency,
        amount=body.amount,
        is_variable=body.is_variable,
        next_due=body.next_due,
        note=body.note,
    )
    db.add(bill)
    db.flush()
    bill_logic.ensure_occurrences(db, bill)
    db.commit()
    return bill_dict(bill)


@router.get("/accrual")
def accrual(on: dt.date | None = None, db: Session = Depends(get_db)):
    """Bills overview data: monthly load per bill (annual/12 etc.),
    set-aside progress, totals by category. ``on`` defaults to today."""
    for bill in db.execute(select(Bill).where(Bill.active)).scalars():
        bill_logic.ensure_occurrences(db, bill)
    db.commit()
    return bill_logic.accrual_summary(db, today=on)


@router.get("/occurrences")
def list_occurrences(
    status: str | None = None,
    start: dt.date | None = None,
    end: dt.date | None = None,
    db: Session = Depends(get_db),
):
    for bill in db.execute(select(Bill).where(Bill.active)).scalars():
        bill_logic.ensure_occurrences(db, bill)
    db.commit()
    query = (
        select(BillOccurrence)
        .join(Bill, BillOccurrence.bill_id == Bill.id)
        .where(Bill.active)
        .order_by(BillOccurrence.due_date)
    )
    if status is not None:
        query = query.where(BillOccurrence.status == status)
    if start is not None:
        query = query.where(BillOccurrence.due_date >= start)
    if end is not None:
        query = query.where(BillOccurrence.due_date <= end)
    return [bill_logic.occurrence_dict(o) for o in db.execute(query).scalars().all()]


@router.get("/{bill_id}")
def get_bill(bill_id: int, db: Session = Depends(get_db)):
    bill = db.get(Bill, bill_id)
    if bill is None:
        raise HTTPException(404, f"no bill {bill_id}")
    data = bill_dict(bill)
    data["occurrences"] = [bill_logic.occurrence_dict(o) for o in bill.occurrences]
    return data


@router.put("/{bill_id}")
def update_bill(bill_id: int, body: BillUpdate, db: Session = Depends(get_db)):
    bill = db.get(Bill, bill_id)
    if bill is None:
        raise HTTPException(404, f"no bill {bill_id}")
    validate_bill_refs(db, None, body.category_id, body.account_id)
    if body.name is not None:
        bill.name = body.name
    if body.category_id is not None:
        bill.category_id = body.category_id
    if body.account_id is not None:
        bill.account_id = body.account_id
    if body.is_variable is not None:
        bill.is_variable = body.is_variable
    if body.active is not None:
        bill.active = body.active
    if body.note is not None:
        bill.note = body.note
    if body.amount is not None:
        if body.amount <= 0:
            raise HTTPException(422, "amount must be positive")
        bill.amount = body.amount
        for occ in bill.occurrences:
            if occ.status == "upcoming":
                occ.expected_amount = body.amount
    if body.next_due is not None:
        bill.next_due = body.next_due
        # Re-anchor: drop unpaid future occurrences and regenerate.
        for occ in list(bill.occurrences):
            if occ.status == "upcoming":
                db.delete(occ)
        db.flush()
        bill_logic.ensure_occurrences(db, bill)
    db.commit()
    return bill_dict(bill)


@router.delete("/{bill_id}")
def delete_bill(bill_id: int, db: Session = Depends(get_db)):
    bill = db.get(Bill, bill_id)
    if bill is not None:
        db.delete(bill)
        db.commit()
    return {"deleted": bill_id}


@router.post("/occurrences/{occurrence_id}/resolve")
def resolve_occurrence(occurrence_id: int, body: ResolveIn, db: Session = Depends(get_db)):
    """Answer the fixed-bill-amount-changed prompt: update the bill or keep."""
    occ = db.get(BillOccurrence, occurrence_id)
    if occ is None:
        raise HTTPException(404, f"no occurrence {occurrence_id}")
    try:
        result = bill_logic.resolve_amount_review(db, occ, body.action)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    db.commit()
    return result


@router.post("/occurrences/{occurrence_id}/skip")
def skip_occurrence(occurrence_id: int, db: Session = Depends(get_db)):
    occ = db.get(BillOccurrence, occurrence_id)
    if occ is None:
        raise HTTPException(404, f"no occurrence {occurrence_id}")
    if occ.status not in ("upcoming", "amount_review"):
        raise HTTPException(422, f"occurrence is {occ.status}")
    occ.status = "skipped"
    db.commit()
    return bill_logic.occurrence_dict(occ)


@router.post("/occurrences/{occurrence_id}/unmatch")
def unmatch_occurrence(occurrence_id: int, db: Session = Depends(get_db)):
    """Undo a wrong match: back to upcoming, transaction link cleared."""
    occ = db.get(BillOccurrence, occurrence_id)
    if occ is None:
        raise HTTPException(404, f"no occurrence {occurrence_id}")
    occ.status = "upcoming"
    occ.matched_transaction_id = None
    occ.actual_amount = None
    occ.expected_amount = occ.bill.amount
    db.commit()
    return bill_logic.occurrence_dict(occ)
