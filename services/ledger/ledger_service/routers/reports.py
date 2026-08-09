from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..db import get_db
from ..logic import reports

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.get("/category-actuals")
def category_actuals(
    start: date,
    end: date,
    db: Session = Depends(get_db),
):
    """Net per category over [start, end]. Normal transactions in cash-flow
    accounts only; reimbursements net against their category."""
    return reports.category_actuals(db, start, end)


@router.get("/cashflow")
def cashflow(
    months: int = Query(12, ge=1, le=120),
    end: date | None = None,
    db: Session = Depends(get_db),
):
    return reports.cashflow_by_month(db, months, end)


@router.get("/net-worth")
def net_worth(
    months: int = Query(12, ge=1, le=120),
    base_currency: str = "USD",
    db: Session = Depends(get_db),
):
    return reports.net_worth_series(db, months, base_currency.upper())
