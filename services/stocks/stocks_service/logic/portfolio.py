"""Portfolio domain logic: applying transactions, FIFO selling, cash,
holdings, and valuations."""

from __future__ import annotations

import calendar
from datetime import date as date_type
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sakura_common.money import money_str, qty_str

from ..models import (
    InvestmentAccount,
    Lot,
    Price,
    Security,
    StockTransaction,
)

ZERO = Decimal("0")


def get_or_create_security(db: Session, symbol: str, name: str = "") -> Security:
    symbol = symbol.upper().strip()
    if not symbol:
        raise HTTPException(422, "symbol is required")
    security = db.execute(select(Security).where(Security.symbol == symbol)).scalar_one_or_none()
    if security is None:
        security = Security(symbol=symbol, name=name)
        db.add(security)
        db.flush()
    return security


def cash_balance(db: Session, account: InvestmentAccount, as_of: date_type | None = None) -> Decimal:
    query = select(func.coalesce(func.sum(StockTransaction.amount), 0)).where(
        StockTransaction.account_id == account.id
    )
    if as_of is not None:
        query = query.where(StockTransaction.date <= as_of)
    movement = db.execute(query).scalar_one()
    return account.opening_cash + Decimal(str(movement))


def latest_price(db: Session, security_id: int, as_of: date_type | None = None) -> Price | None:
    query = (
        select(Price).where(Price.security_id == security_id).order_by(Price.date.desc()).limit(1)
    )
    if as_of is not None:
        query = (
            select(Price)
            .where(Price.security_id == security_id, Price.date <= as_of)
            .order_by(Price.date.desc())
            .limit(1)
        )
    return db.execute(query).scalar_one_or_none()


def apply_transaction(
    db: Session,
    *,
    account: InvestmentAccount,
    type: str,
    date: date_type,
    symbol: str = "",
    quantity: Decimal | None = None,
    price: Decimal | None = None,
    amount: Decimal | None = None,
    fees: Decimal = ZERO,
    note: str = "",
    transfer_group_id: str | None = None,
    external_account: str | None = None,
    import_hash: str | None = None,
) -> StockTransaction:
    """Create one transaction and its side effects (lots, FIFO reduction).
    Adds to the session without committing — callers own the commit."""
    security = get_or_create_security(db, symbol) if symbol else None

    if type in ("buy", "sell", "vest"):
        if security is None or quantity is None or quantity <= 0:
            raise HTTPException(422, f"{type} needs a symbol and a positive quantity")

    txn = StockTransaction(
        account_id=account.id,
        security_id=security.id if security else None,
        type=type,
        date=date,
        quantity=quantity,
        price=price,
        fees=fees or ZERO,
        note=note,
        transfer_group_id=transfer_group_id,
        external_account=external_account,
        import_hash=import_hash,
    )

    if type == "buy":
        if price is None and amount is None:
            raise HTTPException(422, "buy needs a price or a total amount")
        total_cost = -amount if amount is not None else quantity * price + (fees or ZERO)
        if total_cost <= 0:
            raise HTTPException(422, "buy total must be a cost (check the sign convention)")
        txn.amount = -total_cost
        db.add(Lot(
            account_id=account.id,
            security_id=security.id,
            quantity=quantity,
            cost_basis=total_cost,
            acquired_date=date,
            source="buy",
        ))
    elif type == "sell":
        if price is None and amount is None:
            raise HTTPException(422, "sell needs a price or a total amount")
        proceeds = amount if amount is not None else quantity * price - (fees or ZERO)
        if proceeds < 0:
            raise HTTPException(422, "sell proceeds cannot be negative")
        basis_removed = reduce_lots_fifo(db, account.id, security.id, quantity)
        txn.amount = proceeds
        txn.realized_gain = proceeds - basis_removed
    elif type == "vest":
        # Shares appear at market value; no cash moves. Basis = vest-day price.
        vest_price = price
        if vest_price is None:
            row = latest_price(db, security.id, as_of=date)
            vest_price = row.close if row else ZERO
        txn.price = vest_price
        txn.amount = ZERO
        lot = Lot(
            account_id=account.id,
            security_id=security.id,
            quantity=quantity,
            cost_basis=quantity * vest_price,
            acquired_date=date,
            source="vest",
        )
        db.add(lot)
        db.flush()
        txn.note = note or f"Vested {quantity} @ {vest_price}"
        txn._vest_lot = lot  # transient attribute; RSU release links the lot id
    elif type in ("dividend", "deposit"):
        if amount is None or amount <= 0:
            raise HTTPException(422, f"{type} needs a positive amount")
        txn.amount = amount
    elif type == "withdraw":
        if amount is None or amount <= 0:
            raise HTTPException(422, "withdraw needs a positive amount")
        txn.amount = -amount
    elif type == "fee":
        if amount is None or amount <= 0:
            raise HTTPException(422, "fee needs a positive amount")
        txn.amount = -amount
    else:
        raise HTTPException(422, f"unknown transaction type {type!r}")

    db.add(txn)
    db.flush()
    return txn


def reduce_lots_fifo(db: Session, account_id: int, security_id: int, quantity: Decimal) -> Decimal:
    """Remove ``quantity`` shares oldest-first; returns the cost basis
    removed (for realized gain)."""
    lots = (
        db.execute(
            select(Lot)
            .where(Lot.account_id == account_id, Lot.security_id == security_id)
            .order_by(Lot.acquired_date, Lot.id)
        )
        .scalars()
        .all()
    )
    held = sum((lot.quantity for lot in lots), ZERO)
    if quantity > held:
        raise HTTPException(422, f"cannot sell {quantity}: only {held} held")
    remaining = quantity
    basis_removed = ZERO
    for lot in lots:
        if remaining <= 0:
            break
        take = min(lot.quantity, remaining)
        share_of_basis = (lot.cost_basis * take / lot.quantity) if lot.quantity else ZERO
        basis_removed += share_of_basis
        lot.quantity -= take
        lot.cost_basis -= share_of_basis
        remaining -= take
        if lot.quantity == 0:
            db.delete(lot)
    return basis_removed


def holdings(db: Session, account: InvestmentAccount, as_of: date_type | None = None) -> list[dict]:
    """Aggregated positions with latest price and unrealized gain."""
    lots = (
        db.execute(select(Lot).where(Lot.account_id == account.id))
        .scalars()
        .all()
    )
    by_security: dict[int, dict] = {}
    for lot in lots:
        entry = by_security.setdefault(
            lot.security_id,
            {"security": lot.security, "quantity": ZERO, "cost_basis": ZERO},
        )
        entry["quantity"] += lot.quantity
        entry["cost_basis"] += lot.cost_basis
    result = []
    for entry in by_security.values():
        if entry["quantity"] == 0:
            continue
        security = entry["security"]
        price_row = latest_price(db, security.id, as_of)
        market_value = entry["quantity"] * price_row.close if price_row else None
        unrealized = market_value - entry["cost_basis"] if market_value is not None else None
        result.append(
            {
                "symbol": security.symbol,
                "name": security.name,
                "security_id": security.id,
                "quantity": qty_str(entry["quantity"]),
                "cost_basis": money_str(entry["cost_basis"]),
                "last_price": money_str(price_row.close) if price_row else None,
                "price_date": price_row.date.isoformat() if price_row else None,
                "price_source": price_row.source if price_row else None,
                "market_value": money_str(market_value),
                "unrealized_gain": money_str(unrealized),
            }
        )
    result.sort(key=lambda row: row["symbol"])
    return result


def account_valuation(db: Session, account: InvestmentAccount, as_of: date_type | None = None) -> dict:
    cash = cash_balance(db, account, as_of)
    positions = holdings(db, account, as_of)
    market = ZERO
    priced = True
    for position in positions:
        if position["market_value"] is None:
            priced = False
            continue
        market += Decimal(position["market_value"])
    return {
        "account_id": account.id,
        "name": account.name,
        "type": account.type,
        "cash": money_str(cash),
        "market_value": money_str(market),
        "total": money_str(cash + market),
        "fully_priced": priced,
    }


def valuation_series(db: Session, months: int) -> list[dict]:
    """Month-end total (cash + holdings at latest price <= month end) across
    all active accounts — feeds the web-ui net worth report."""
    accounts = db.execute(
        select(InvestmentAccount).where(InvestmentAccount.active)
    ).scalars().all()
    today = date_type.today()
    series = []
    for offset in range(months - 1, -1, -1):
        index = today.year * 12 + (today.month - 1) - offset
        year, month = index // 12, index % 12 + 1
        month_end = date_type(year, month, calendar.monthrange(year, month)[1])
        total = ZERO
        for account in accounts:
            value = account_valuation(db, account, as_of=month_end)
            total += Decimal(value["total"])
        series.append({"month": f"{year:04d}-{month:02d}", "total": money_str(total)})
    return series
