"""Currencies and FX rates."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from sakura_common.money import money_str

from ..db import get_db
from ..models import Currency, FxRate
from ..serialize import fx_dict
from ..logic import fx as fx_logic

router = APIRouter(prefix="/api", tags=["meta"])


class CurrencyIn(BaseModel):
    code: str
    name: str
    decimals: int = 2


class FxRateIn(BaseModel):
    date: dt.date
    from_code: str
    to_code: str
    rate: Decimal


@router.get("/currencies")
def list_currencies(db: Session = Depends(get_db)):
    rows = db.execute(select(Currency).order_by(Currency.code)).scalars().all()
    return [{"code": c.code, "name": c.name, "decimals": c.decimals} for c in rows]


@router.post("/currencies")
def add_currency(body: CurrencyIn, db: Session = Depends(get_db)):
    code = body.code.upper()
    if len(code) != 3 or not code.isalpha():
        raise HTTPException(422, "currency code must be 3 letters (ISO 4217)")
    if db.get(Currency, code) is not None:
        raise HTTPException(409, f"currency {code} already exists")
    db.add(Currency(code=code, name=body.name, decimals=body.decimals))
    db.commit()
    return {"code": code, "name": body.name, "decimals": body.decimals}


@router.get("/fx")
def list_fx(db: Session = Depends(get_db)):
    rows = db.execute(
        select(FxRate).order_by(FxRate.date.desc(), FxRate.from_code)
    ).scalars().all()
    return [fx_dict(r) for r in rows]


@router.post("/fx")
def add_fx(body: FxRateIn, db: Session = Depends(get_db)):
    from_code, to_code = body.from_code.upper(), body.to_code.upper()
    for code in (from_code, to_code):
        if db.get(Currency, code) is None:
            raise HTTPException(422, f"unknown currency {code} — add it first")
    if from_code == to_code:
        raise HTTPException(422, "from and to currency must differ")
    if body.rate <= 0:
        raise HTTPException(422, "rate must be positive")
    existing = db.execute(
        select(FxRate).where(
            FxRate.date == body.date, FxRate.from_code == from_code, FxRate.to_code == to_code
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.rate = body.rate
        db.commit()
        return fx_dict(existing)
    row = FxRate(date=body.date, from_code=from_code, to_code=to_code, rate=body.rate)
    db.add(row)
    db.commit()
    return fx_dict(row)


@router.delete("/fx/{fx_id}")
def delete_fx(fx_id: int, db: Session = Depends(get_db)):
    row = db.get(FxRate, fx_id)
    if row is not None:
        db.delete(row)
        db.commit()
    return {"deleted": fx_id}


@router.get("/fx/rate")
def get_rate(from_code: str, to_code: str, on: dt.date | None = None, db: Session = Depends(get_db)):
    rate = fx_logic.get_rate(db, from_code.upper(), to_code.upper(), on or dt.date.today())
    return {"rate": money_str(rate)}
