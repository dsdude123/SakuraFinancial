from __future__ import annotations

import datetime as dt
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..logic.balances import account_balance
from ..logic.transactions import set_asset_value
from ..models import ACCOUNT_TYPES, Account, Currency, Payee, Transaction
from ..serialize import account_dict, transaction_dict

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


class AccountIn(BaseModel):
    name: str
    type: str
    currency_code: str = "USD"
    opening_balance: Decimal = Decimal("0")
    note: str = ""


class AccountUpdate(BaseModel):
    name: str | None = None
    type: str | None = None
    currency_code: str | None = None
    opening_balance: Decimal | None = None
    note: str | None = None
    active: bool | None = None


class ValuationIn(BaseModel):
    date: dt.date
    new_value: Decimal


# Fees, interest and other charges are levied by the institution itself, so
# the payee for them is the account. Auto-creating it means a "Foreign
# Transaction Fee 76" row has somewhere obvious to go on the very first import.
CHARGING_TYPES = ("checking", "savings", "credit_card", "cash")


def ensure_account_payee(db: Session, account: Account) -> Payee | None:
    """Give an account a same-named payee to book its own charges against."""
    if account.type not in CHARGING_TYPES:
        return None
    existing = db.execute(
        select(Payee).where(Payee.name == account.name)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    payee = Payee(name=account.name)
    db.add(payee)
    db.flush()
    return payee


def get_account_or_404(db: Session, account_id: int) -> Account:
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(404, f"no account {account_id}")
    return account


@router.get("")
def list_accounts(include_inactive: bool = False, db: Session = Depends(get_db)):
    query = select(Account).order_by(Account.type, Account.name)
    if not include_inactive:
        query = query.where(Account.active)
    accounts = db.execute(query).scalars().all()
    return [account_dict(a, balance=account_balance(db, a)) for a in accounts]


@router.post("")
def create_account(body: AccountIn, db: Session = Depends(get_db)):
    if body.type not in ACCOUNT_TYPES:
        raise HTTPException(422, f"type must be one of {ACCOUNT_TYPES}")
    code = body.currency_code.upper()
    if db.get(Currency, code) is None:
        raise HTTPException(422, f"unknown currency {code} — add it first")
    if db.execute(select(Account).where(Account.name == body.name)).scalar_one_or_none():
        raise HTTPException(409, f"account named {body.name!r} already exists")
    account = Account(
        name=body.name,
        type=body.type,
        currency_code=code,
        opening_balance=body.opening_balance,
        note=body.note,
    )
    db.add(account)
    db.flush()
    ensure_account_payee(db, account)
    db.commit()
    return account_dict(account, balance=account_balance(db, account))


@router.get("/{account_id}")
def get_account(account_id: int, db: Session = Depends(get_db)):
    account = get_account_or_404(db, account_id)
    return account_dict(account, balance=account_balance(db, account))


@router.put("/{account_id}")
def update_account(account_id: int, body: AccountUpdate, db: Session = Depends(get_db)):
    account = get_account_or_404(db, account_id)
    if body.name is not None and body.name != account.name:
        clash = db.execute(select(Account).where(Account.name == body.name)).scalar_one_or_none()
        if clash is not None:
            raise HTTPException(409, f"account named {body.name!r} already exists")
        account.name = body.name
    if body.type is not None and body.type != account.type:
        if body.type not in ACCOUNT_TYPES:
            raise HTTPException(422, f"type must be one of {ACCOUNT_TYPES}")
        account.type = body.type
    if body.currency_code is not None:
        code = body.currency_code.upper()
        if db.get(Currency, code) is None:
            raise HTTPException(422, f"unknown currency {code} - add it first")
        if code != account.currency_code:
            posted = db.execute(
                select(func.count()).select_from(Transaction)
                .where(Transaction.account_id == account_id)
            ).scalar_one()
            if posted:
                raise HTTPException(
                    409,
                    f"account has {posted} transactions in {account.currency_code} - "
                    "changing its currency would silently reinterpret every amount",
                )
            account.currency_code = code
    if body.opening_balance is not None:
        account.opening_balance = body.opening_balance
    if body.note is not None:
        account.note = body.note
    if body.active is not None:
        account.active = body.active
    db.commit()
    return account_dict(account, balance=account_balance(db, account))


@router.delete("/{account_id}")
def delete_account(account_id: int, db: Session = Depends(get_db)):
    account = get_account_or_404(db, account_id)
    in_use = db.execute(
        select(func.count()).select_from(Transaction).where(Transaction.account_id == account_id)
    ).scalar_one()
    if in_use:
        raise HTTPException(
            409, f"account has {in_use} transactions — deactivate it instead of deleting"
        )
    db.delete(account)
    db.commit()
    return {"deleted": account_id}


@router.post("/{account_id}/valuation")
def record_valuation(account_id: int, body: ValuationIn, db: Session = Depends(get_db)):
    """Set an asset/liability account's current value. The delta is recorded
    as a kind='valuation' transaction: net worth moves, cash flow does not."""
    account = get_account_or_404(db, account_id)
    current = account_balance(db, account, as_of=body.date)
    txn = set_asset_value(
        db, account=account, date=body.date, new_value=body.new_value, current_balance=current
    )
    db.commit()
    return transaction_dict(txn)
