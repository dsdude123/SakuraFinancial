from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sakura_common.dedup import normalize_description

from ..db import get_db
from ..models import ALIAS_MATCH_TYPES, Category, Payee, PayeeAlias, Transaction
from ..serialize import alias_dict, payee_dict

router = APIRouter(prefix="/api/payees", tags=["payees"])


class PayeeIn(BaseModel):
    name: str
    default_category_id: int | None = None


class PayeeUpdate(BaseModel):
    name: str | None = None
    default_category_id: int | None = None
    active: bool | None = None


class AliasIn(BaseModel):
    pattern: str
    match_type: str = "exact"


def get_payee_or_404(db: Session, payee_id: int) -> Payee:
    payee = db.get(Payee, payee_id)
    if payee is None:
        raise HTTPException(404, f"no payee {payee_id}")
    return payee


def last_transaction_details(db: Session, payee_id: int):
    """The payee's most recent normal transaction — powers form auto-fill
    (category + amount pre-filled the way MS Money did it)."""
    txn = db.execute(
        select(Transaction)
        .where(Transaction.payee_id == payee_id, Transaction.kind == "normal")
        .order_by(Transaction.date.desc(), Transaction.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if txn is None:
        return None, None
    first_split = txn.splits[0] if txn.splits else None
    return txn.total, (first_split.category_id if first_split else None)


@router.get("")
def list_payees(include_inactive: bool = False, db: Session = Depends(get_db)):
    query = select(Payee).order_by(Payee.name)
    if not include_inactive:
        query = query.where(Payee.active)
    return [payee_dict(p) for p in db.execute(query).scalars().all()]


@router.post("")
def create_payee(body: PayeeIn, db: Session = Depends(get_db)):
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "payee name is required")
    if db.execute(select(Payee).where(Payee.name == name)).scalar_one_or_none():
        raise HTTPException(409, f"payee {name!r} already exists")
    if body.default_category_id is not None and db.get(Category, body.default_category_id) is None:
        raise HTTPException(422, f"no category {body.default_category_id}")
    payee = Payee(name=name, default_category_id=body.default_category_id)
    db.add(payee)
    db.commit()
    return payee_dict(payee)


@router.get("/{payee_id}")
def get_payee(payee_id: int, db: Session = Depends(get_db)):
    """Includes last_amount / last_category_id for entry-form auto-fill."""
    payee = get_payee_or_404(db, payee_id)
    last_amount, last_category_id = last_transaction_details(db, payee_id)
    return payee_dict(payee, last_amount=last_amount, last_category_id=last_category_id)


@router.put("/{payee_id}")
def update_payee(payee_id: int, body: PayeeUpdate, db: Session = Depends(get_db)):
    payee = get_payee_or_404(db, payee_id)
    if body.name is not None and body.name != payee.name:
        clash = db.execute(select(Payee).where(Payee.name == body.name)).scalar_one_or_none()
        if clash is not None:
            raise HTTPException(409, f"payee {body.name!r} already exists")
        payee.name = body.name
    if body.default_category_id is not None:
        if db.get(Category, body.default_category_id) is None:
            raise HTTPException(422, f"no category {body.default_category_id}")
        payee.default_category_id = body.default_category_id
    if body.active is not None:
        payee.active = body.active
    db.commit()
    return payee_dict(payee)


@router.delete("/{payee_id}")
def delete_payee(payee_id: int, db: Session = Depends(get_db)):
    payee = get_payee_or_404(db, payee_id)
    in_use = db.execute(
        select(func.count()).select_from(Transaction).where(Transaction.payee_id == payee_id)
    ).scalar_one()
    if in_use:
        raise HTTPException(409, f"payee has {in_use} transactions — deactivate instead")
    db.execute(PayeeAlias.__table__.delete().where(PayeeAlias.payee_id == payee_id))
    db.delete(payee)
    db.commit()
    return {"deleted": payee_id}


@router.get("/{payee_id}/aliases")
def list_aliases(payee_id: int, db: Session = Depends(get_db)):
    get_payee_or_404(db, payee_id)
    aliases = db.execute(
        select(PayeeAlias).where(PayeeAlias.payee_id == payee_id).order_by(PayeeAlias.pattern)
    ).scalars().all()
    return [alias_dict(a) for a in aliases]


@router.post("/{payee_id}/aliases")
def add_alias(payee_id: int, body: AliasIn, db: Session = Depends(get_db)):
    get_payee_or_404(db, payee_id)
    if body.match_type not in ALIAS_MATCH_TYPES:
        raise HTTPException(422, f"match_type must be one of {ALIAS_MATCH_TYPES}")
    pattern = normalize_description(body.pattern)
    if not pattern:
        raise HTTPException(422, "pattern is required")
    existing = db.execute(
        select(PayeeAlias).where(
            PayeeAlias.pattern == pattern, PayeeAlias.match_type == body.match_type
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Re-learning an alias re-points it: the user corrected a mapping.
        existing.payee_id = payee_id
        db.commit()
        return alias_dict(existing)
    alias = PayeeAlias(payee_id=payee_id, pattern=pattern, match_type=body.match_type)
    db.add(alias)
    db.commit()
    return alias_dict(alias)


@router.delete("/{payee_id}/aliases/{alias_id}")
def delete_alias(payee_id: int, alias_id: int, db: Session = Depends(get_db)):
    alias = db.get(PayeeAlias, alias_id)
    if alias is not None and alias.payee_id == payee_id:
        db.delete(alias)
        db.commit()
    return {"deleted": alias_id}
