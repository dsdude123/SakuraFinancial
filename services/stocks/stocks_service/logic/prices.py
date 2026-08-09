"""Price history: daily Yahoo fetch, backfill, manual entry."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from sakura_common.yahoo import YahooClient, YahooError

from ..models import Price, Security

logger = logging.getLogger(__name__)

BACKFILL_YEARS = 5


def upsert_price(
    db: Session, security: Security, day: date, close: Decimal, source: str = "yahoo"
) -> Price:
    row = db.execute(
        select(Price).where(Price.security_id == security.id, Price.date == day)
    ).scalar_one_or_none()
    if row is None:
        row = Price(security_id=security.id, date=day, close=close, source=source)
        db.add(row)
    else:
        # Manual entries win over automated ones; Yahoo can correct Yahoo.
        if row.source == "manual" and source == "yahoo":
            return row
        row.close = close
        row.source = source
    return row


def backfill_security(db: Session, yahoo: YahooClient, security: Security) -> int:
    """Fetch several years of daily history for a newly added security so
    charts and valuations have depth from day one. Best-effort."""
    start = date.today() - timedelta(days=365 * BACKFILL_YEARS)
    try:
        history = yahoo.daily_history(security.symbol, start, date.today())
    except YahooError as exc:
        logger.warning("backfill failed for %s: %s", security.symbol, exc)
        return 0
    for day, close in history:
        upsert_price(db, security, day, close, source="yahoo")
    return len(history)


def refresh_all(db: Session, yahoo: YahooClient) -> dict:
    """The daily job: previous close for every active security. Partial
    failures don't stop the rest (Yahoo hiccups on individual symbols)."""
    securities = db.execute(select(Security).where(Security.active)).scalars().all()
    updated: list[str] = []
    failed: list[str] = []
    for security in securities:
        try:
            day, close = yahoo.latest_close(security.symbol)
        except YahooError as exc:
            logger.warning("price refresh failed for %s: %s", security.symbol, exc)
            failed.append(security.symbol)
            continue
        upsert_price(db, security, day, close, source="yahoo")
        updated.append(security.symbol)
    return {"updated": updated, "failed": failed}
