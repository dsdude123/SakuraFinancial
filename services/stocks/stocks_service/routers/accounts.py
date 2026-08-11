"""Accounts, transactions, holdings, valuations, and RSU grants/vesting."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from sakura_common.money import money_str, qty_str

from ..db import get_db
from ..logic import portfolio
from ..models import (
    ACCOUNT_TYPES,
    InvestmentAccount,
    RsuGrant,
    StockTransaction,
    TXN_TYPES,
    VestingEvent,
)

router = APIRouter(prefix="/api", tags=["accounts"])


class AccountIn(BaseModel):
    name: str
    type: str
    currency: str = "USD"
    opening_cash: Decimal = Decimal("0")
    trading_window_open: dt.date | None = None
    trading_window_close: dt.date | None = None
    note: str = ""


class AccountUpdate(BaseModel):
    name: str | None = None
    opening_cash: Decimal | None = None
    trading_window_open: dt.date | None = None
    trading_window_close: dt.date | None = None
    active: bool | None = None
    note: str | None = None


class TxnIn(BaseModel):
    account_id: int
    type: str
    date: dt.date
    symbol: str = ""
    quantity: Decimal | None = None
    price: Decimal | None = None
    amount: Decimal | None = None
    fees: Decimal = Decimal("0")
    note: str = ""


class GrantIn(BaseModel):
    account_id: int
    symbol: str
    grant_date: dt.date
    note: str = ""
    vesting: list[dict]  # [{"vest_date": "YYYY-MM-DD", "shares": "25"}]


class ReleaseIn(BaseModel):
    price: Decimal | None = None


def account_dict(db: Session, account: InvestmentAccount) -> dict:
    valuation = portfolio.account_valuation(db, account)
    return {
        "id": account.id,
        "name": account.name,
        "type": account.type,
        "currency": account.currency,
        "opening_cash": money_str(account.opening_cash),
        "cash": valuation["cash"],
        "market_value": valuation["market_value"],
        "total": valuation["total"],
        "fully_priced": valuation["fully_priced"],
        "trading_window_open": account.trading_window_open.isoformat()
        if account.trading_window_open
        else None,
        "trading_window_close": account.trading_window_close.isoformat()
        if account.trading_window_close
        else None,
        "active": account.active,
        "note": account.note,
    }


def txn_dict(txn: StockTransaction) -> dict:
    return {
        "id": txn.id,
        "account_id": txn.account_id,
        "type": txn.type,
        "date": txn.date.isoformat(),
        "symbol": txn.security.symbol if txn.security else None,
        "quantity": qty_str(txn.quantity),
        "price": money_str(txn.price),
        "amount": money_str(txn.amount),
        "fees": money_str(txn.fees),
        "realized_gain": money_str(txn.realized_gain),
        "note": txn.note,
    }


def get_account_or_404(db: Session, account_id: int) -> InvestmentAccount:
    account = db.get(InvestmentAccount, account_id)
    if account is None:
        raise HTTPException(404, f"no investment account {account_id}")
    return account


@router.get("/accounts")
def list_accounts(include_inactive: bool = False, db: Session = Depends(get_db)):
    query = select(InvestmentAccount).order_by(InvestmentAccount.name)
    if not include_inactive:
        query = query.where(InvestmentAccount.active)
    return [account_dict(db, account) for account in db.execute(query).scalars()]


@router.post("/accounts")
def create_account(body: AccountIn, db: Session = Depends(get_db)):
    if body.type not in ACCOUNT_TYPES:
        raise HTTPException(422, f"type must be one of {ACCOUNT_TYPES}")
    clash = db.execute(
        select(InvestmentAccount).where(InvestmentAccount.name == body.name)
    ).scalar_one_or_none()
    if clash is not None:
        raise HTTPException(409, f"account {body.name!r} already exists")
    account = InvestmentAccount(**body.model_dump())
    db.add(account)
    db.commit()
    return account_dict(db, account)


@router.get("/accounts/{account_id}")
def get_account(account_id: int, db: Session = Depends(get_db)):
    account = get_account_or_404(db, account_id)
    data = account_dict(db, account)
    data["holdings"] = portfolio.holdings(db, account)
    return data


@router.put("/accounts/{account_id}")
def update_account(account_id: int, body: AccountUpdate, db: Session = Depends(get_db)):
    account = get_account_or_404(db, account_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(account, field, value)
    db.commit()
    return account_dict(db, account)


@router.get("/transactions")
def list_transactions(
    account_id: int | None = None,
    limit: int = Query(100, le=1000),
    offset: int = 0,
    db: Session = Depends(get_db),
):
    query = select(StockTransaction).order_by(
        StockTransaction.date.desc(), StockTransaction.id.desc()
    )
    if account_id is not None:
        query = query.where(StockTransaction.account_id == account_id)
    rows = db.execute(query.limit(limit).offset(offset)).scalars().all()
    return [txn_dict(txn) for txn in rows]


@router.post("/transactions")
def create_transaction(body: TxnIn, db: Session = Depends(get_db)):
    account = get_account_or_404(db, body.account_id)
    if body.type not in TXN_TYPES:
        raise HTTPException(422, f"type must be one of {TXN_TYPES}")
    txn = portfolio.apply_transaction(
        db,
        account=account,
        type=body.type,
        date=body.date,
        symbol=body.symbol,
        quantity=body.quantity,
        price=body.price,
        amount=body.amount,
        fees=body.fees,
        note=body.note,
    )
    db.commit()
    return txn_dict(txn)


@router.get("/valuation")
def valuation(on: dt.date | None = None, db: Session = Depends(get_db)):
    accounts = db.execute(
        select(InvestmentAccount).where(InvestmentAccount.active)
    ).scalars().all()
    rows = [portfolio.account_valuation(db, account, as_of=on) for account in accounts]
    total = sum((Decimal(row["total"]) for row in rows), Decimal("0"))
    return {"accounts": rows, "total": money_str(total)}


@router.get("/valuation/series")
def valuation_series(months: int = Query(24, ge=1, le=120), db: Session = Depends(get_db)):
    return portfolio.valuation_series(db, months)


@router.get("/rsu/grants")
def list_grants(account_id: int | None = None, db: Session = Depends(get_db)):
    query = select(RsuGrant).order_by(RsuGrant.grant_date)
    if account_id is not None:
        query = query.where(RsuGrant.account_id == account_id)
    grants = db.execute(query).scalars().all()
    return [
        {
            "id": grant.id,
            "account_id": grant.account_id,
            "symbol": grant.security.symbol,
            "grant_date": grant.grant_date.isoformat(),
            "note": grant.note,
            "vesting": [
                {
                    "id": event.id,
                    "vest_date": event.vest_date.isoformat(),
                    "shares": qty_str(event.shares),
                    "released": event.released,
                    "due": (not event.released) and event.vest_date <= dt.date.today(),
                }
                for event in grant.vesting_events
            ],
        }
        for grant in grants
    ]


@router.post("/rsu/grants")
def create_grant(body: GrantIn, db: Session = Depends(get_db)):
    account = get_account_or_404(db, body.account_id)
    if account.type != "rsu":
        raise HTTPException(422, "grants belong on an RSU account")
    if not body.vesting:
        raise HTTPException(422, "a grant needs at least one vesting event")
    security = portfolio.get_or_create_security(db, body.symbol)
    grant = RsuGrant(
        account_id=account.id,
        security_id=security.id,
        grant_date=body.grant_date,
        note=body.note,
    )
    for event in body.vesting:
        try:
            grant.vesting_events.append(
                VestingEvent(
                    vest_date=dt.date.fromisoformat(str(event["vest_date"])),
                    shares=Decimal(str(event["shares"])),
                )
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(422, f"bad vesting entry {event!r}: {exc}")
    db.add(grant)
    db.commit()
    return {"id": grant.id, "vesting_events": len(grant.vesting_events)}


@router.post("/rsu/vests/{vest_id}/release")
def release_vest(vest_id: int, body: ReleaseIn, db: Session = Depends(get_db)):
    """Mark a vesting event released: shares become a lot at vest-day market
    value (price parameter overrides the stored price history)."""
    event = db.get(VestingEvent, vest_id)
    if event is None:
        raise HTTPException(404, f"no vesting event {vest_id}")
    if event.released:
        raise HTTPException(422, "already released")
    grant = event.grant
    account = db.get(InvestmentAccount, grant.account_id)
    txn = portfolio.apply_transaction(
        db,
        account=account,
        type="vest",
        date=event.vest_date,
        symbol=grant.security.symbol,
        quantity=event.shares,
        price=body.price,
    )
    event.released = True
    lot = getattr(txn, "_vest_lot", None)
    if lot is not None:
        event.lot_id = lot.id
    db.commit()
    return {"released": vest_id, "transaction": txn_dict(txn)}
