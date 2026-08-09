"""Prices (history, manual entry, refresh) and on-demand analysis."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from sakura_common.money import money_str

from ..db import get_db, get_settings_client, get_yahoo
from ..logic import analysis as analysis_logic
from ..logic import portfolio, prices as price_logic
from ..models import AnalysisResult, InvestmentAccount, Price, Security

router = APIRouter(prefix="/api", tags=["market"])


class ManualPriceIn(BaseModel):
    symbol: str
    date: dt.date
    close: Decimal


class AnalyzeIn(BaseModel):
    account_id: int


@router.get("/securities")
def list_securities(db: Session = Depends(get_db)):
    rows = db.execute(select(Security).order_by(Security.symbol)).scalars().all()
    result = []
    for security in rows:
        last = portfolio.latest_price(db, security.id)
        result.append(
            {
                "id": security.id,
                "symbol": security.symbol,
                "name": security.name,
                "last_price": money_str(last.close) if last else None,
                "price_date": last.date.isoformat() if last else None,
                "price_source": last.source if last else None,
            }
        )
    return result


@router.get("/prices/{symbol}")
def price_history(symbol: str, months: int = Query(12, ge=1, le=120), db: Session = Depends(get_db)):
    security = db.execute(
        select(Security).where(Security.symbol == symbol.upper())
    ).scalar_one_or_none()
    if security is None:
        raise HTTPException(404, f"unknown security {symbol!r}")
    since = dt.date.today() - dt.timedelta(days=months * 31)
    rows = db.execute(
        select(Price)
        .where(Price.security_id == security.id, Price.date >= since)
        .order_by(Price.date)
    ).scalars().all()
    return [
        {"date": row.date.isoformat(), "close": money_str(row.close), "source": row.source}
        for row in rows
    ]


@router.post("/prices")
def add_manual_price(body: ManualPriceIn, db: Session = Depends(get_db)):
    security = portfolio.get_or_create_security(db, body.symbol)
    if body.close <= 0:
        raise HTTPException(422, "close must be positive")
    row = price_logic.upsert_price(db, security, body.date, body.close, source="manual")
    db.commit()
    return {"symbol": security.symbol, "date": row.date.isoformat(), "close": money_str(row.close)}


@router.post("/prices/refresh")
def refresh_prices(db: Session = Depends(get_db), yahoo=Depends(get_yahoo)):
    """Fetch the latest close for every security now (same as the daily job)."""
    result = price_logic.refresh_all(db, yahoo)
    db.commit()
    return result


@router.post("/prices/backfill/{symbol}")
def backfill(symbol: str, db: Session = Depends(get_db), yahoo=Depends(get_yahoo)):
    security = portfolio.get_or_create_security(db, symbol)
    count = price_logic.backfill_security(db, yahoo, security)
    db.commit()
    return {"symbol": security.symbol, "prices_loaded": count}


@router.post("/analyze")
def analyze(
    body: AnalyzeIn,
    db: Session = Depends(get_db),
    yahoo=Depends(get_yahoo),
    settings_client=Depends(get_settings_client),
):
    """On-demand analysis (never scheduled): Yahoo fundamentals + optional
    LLM guidance shaped by the user's stored trading philosophy."""
    account = db.get(InvestmentAccount, body.account_id)
    if account is None:
        raise HTTPException(404, f"no account {body.account_id}")
    if account.type == "managed":
        raise HTTPException(
            422,
            "managed accounts don't get analysis — you can't act on it there; "
            "performance tracking is on the account page",
        )
    result = analysis_logic.analyze_account(db, yahoo, settings_client, account)
    db.commit()
    return analysis_dict(result)


@router.get("/analyses")
def list_analyses(account_id: int | None = None, db: Session = Depends(get_db)):
    query = select(AnalysisResult).order_by(AnalysisResult.created_at.desc()).limit(20)
    if account_id is not None:
        query = query.where(AnalysisResult.account_id == account_id)
    return [analysis_dict(row) for row in db.execute(query).scalars()]


@router.get("/analyses/{analysis_id}")
def get_analysis(analysis_id: int, db: Session = Depends(get_db)):
    row = db.get(AnalysisResult, analysis_id)
    if row is None:
        raise HTTPException(404, f"no analysis {analysis_id}")
    return analysis_dict(row)


def analysis_dict(row: AnalysisResult) -> dict:
    return {
        "id": row.id,
        "account_id": row.account_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "fundamentals": row.fundamentals,
        "ai_text": row.ai_text,
    }
