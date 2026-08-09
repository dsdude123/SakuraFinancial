"""Currency conversion from the manually entered fx_rates table."""

from __future__ import annotations

from datetime import date as date_type
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import FxRate

ONE = Decimal("1")


def get_rate(db: Session, from_code: str, to_code: str, on: date_type) -> Decimal | None:
    """Latest rate on/before ``on``; falls back to inverting the reverse pair.
    Returns None when no rate exists in either direction (caller decides how
    loudly to complain)."""
    if from_code == to_code:
        return ONE
    direct = db.execute(
        select(FxRate)
        .where(FxRate.from_code == from_code, FxRate.to_code == to_code, FxRate.date <= on)
        .order_by(FxRate.date.desc())
        .limit(1)
    ).scalar_one_or_none()
    if direct is not None:
        return direct.rate
    inverse = db.execute(
        select(FxRate)
        .where(FxRate.from_code == to_code, FxRate.to_code == from_code, FxRate.date <= on)
        .order_by(FxRate.date.desc())
        .limit(1)
    ).scalar_one_or_none()
    if inverse is not None and inverse.rate != 0:
        return ONE / inverse.rate
    return None


def convert(
    db: Session, amount: Decimal, from_code: str, to_code: str, on: date_type
) -> tuple[Decimal, bool]:
    """Convert ``amount``; returns (converted, rate_was_found). With no rate
    the amount passes through unchanged and the False flag lets reports show
    a 'missing FX rate' warning instead of silently lying."""
    rate = get_rate(db, from_code, to_code, on)
    if rate is None:
        return amount, False
    return amount * rate, True
