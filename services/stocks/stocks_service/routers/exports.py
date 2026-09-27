"""Whole-database export/import for backup and disaster recovery."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from sakura_common import jsonutil
from sakura_common.money import money_str, qty_str

from ..db import get_db
from ..models import (
    AnalysisResult,
    InvestmentAccount,
    Lot,
    Price,
    RsuGrant,
    Security,
    StockImportProfile,
    StockTransaction,
    VestingEvent,
)

router = APIRouter(prefix="/api", tags=["export"])

TABLES_DELETE_ORDER = [
    "analysis_results",
    "stock_import_rows",
    "stock_import_batches",
    "stock_import_profiles",
    "vesting_events",
    "rsu_grants",
    "prices",
    "stock_transactions",
    "lots",
    "securities",
    "investment_accounts",
]


@router.get("/export")
def export(db: Session = Depends(get_db)):
    return {
        "service": "stocks",
        "investment_accounts": [
            {
                "id": a.id,
                "name": a.name,
                "type": a.type,
                "currency": a.currency,
                "opening_cash": money_str(a.opening_cash),
                "trading_window_open": a.trading_window_open.isoformat()
                if a.trading_window_open
                else None,
                "trading_window_close": a.trading_window_close.isoformat()
                if a.trading_window_close
                else None,
                "active": a.active,
                "note": a.note,
            }
            for a in db.execute(select(InvestmentAccount).order_by(InvestmentAccount.id)).scalars()
        ],
        "securities": [
            {"id": s.id, "symbol": s.symbol, "name": s.name, "active": s.active}
            for s in db.execute(select(Security).order_by(Security.id)).scalars()
        ],
        "lots": [
            {
                "id": lot.id,
                "account_id": lot.account_id,
                "security_id": lot.security_id,
                "quantity": qty_str(lot.quantity),
                "cost_basis": money_str(lot.cost_basis),
                "acquired_date": lot.acquired_date.isoformat(),
                "source": lot.source,
            }
            for lot in db.execute(select(Lot).order_by(Lot.id)).scalars()
        ],
        "stock_transactions": [
            {
                "id": t.id,
                "account_id": t.account_id,
                "security_id": t.security_id,
                "type": t.type,
                "date": t.date.isoformat(),
                "quantity": qty_str(t.quantity),
                "price": money_str(t.price),
                "amount": money_str(t.amount),
                "fees": money_str(t.fees),
                "realized_gain": money_str(t.realized_gain),
                "note": t.note,
                "import_hash": t.import_hash,
            }
            for t in db.execute(select(StockTransaction).order_by(StockTransaction.id)).scalars()
        ],
        "prices": [
            {
                "id": p.id,
                "security_id": p.security_id,
                "date": p.date.isoformat(),
                "close": money_str(p.close),
                "source": p.source,
            }
            for p in db.execute(select(Price).order_by(Price.id)).scalars()
        ],
        "rsu_grants": [
            {
                "id": g.id,
                "account_id": g.account_id,
                "security_id": g.security_id,
                "grant_date": g.grant_date.isoformat(),
                "note": g.note,
                "vesting_events": [
                    {
                        "id": e.id,
                        "vest_date": e.vest_date.isoformat(),
                        "shares": qty_str(e.shares),
                        "released": e.released,
                        "lot_id": e.lot_id,
                    }
                    for e in g.vesting_events
                ],
            }
            for g in db.execute(select(RsuGrant).order_by(RsuGrant.id)).scalars()
        ],
        "stock_import_profiles": [
            {"id": p.id, "name": p.name, "account_id": p.account_id, "config": p.config}
            for p in db.execute(
                select(StockImportProfile).order_by(StockImportProfile.id)
            ).scalars()
        ],
    }


@router.post("/import")
def import_(data: dict, db: Session = Depends(get_db)):
    for key in ("investment_accounts", "securities", "lots", "stock_transactions", "prices"):
        if not isinstance(data.get(key), list):
            raise HTTPException(422, f"export data missing list {key!r}")
    for table in TABLES_DELETE_ORDER:
        db.execute(text(f"DELETE FROM {table}"))
    for a in data["investment_accounts"]:
        db.add(
            InvestmentAccount(
                id=a["id"],
                name=a["name"],
                type=a["type"],
                currency=a.get("currency", "USD"),
                opening_cash=jsonutil.parse_decimal(a.get("opening_cash", "0")),
                trading_window_open=jsonutil.parse_date(a.get("trading_window_open")),
                trading_window_close=jsonutil.parse_date(a.get("trading_window_close")),
                active=a.get("active", True),
                note=a.get("note", ""),
            )
        )
    for s in data["securities"]:
        db.add(Security(id=s["id"], symbol=s["symbol"], name=s.get("name", ""), active=s.get("active", True)))
    for lot in data["lots"]:
        db.add(
            Lot(
                id=lot["id"],
                account_id=lot["account_id"],
                security_id=lot["security_id"],
                quantity=jsonutil.parse_decimal(lot["quantity"]),
                cost_basis=jsonutil.parse_decimal(lot["cost_basis"]),
                acquired_date=jsonutil.parse_date(lot["acquired_date"]),
                source=lot.get("source", "buy"),
            )
        )
    for t in data["stock_transactions"]:
        db.add(
            StockTransaction(
                id=t["id"],
                account_id=t["account_id"],
                security_id=t.get("security_id"),
                type=t["type"],
                date=jsonutil.parse_date(t["date"]),
                quantity=jsonutil.parse_decimal(t.get("quantity")),
                price=jsonutil.parse_decimal(t.get("price")),
                amount=jsonutil.parse_decimal(t.get("amount", "0")),
                fees=jsonutil.parse_decimal(t.get("fees", "0")),
                realized_gain=jsonutil.parse_decimal(t.get("realized_gain")),
                note=t.get("note", ""),
                import_hash=t.get("import_hash"),
            )
        )
    for p in data["prices"]:
        db.add(
            Price(
                id=p["id"],
                security_id=p["security_id"],
                date=jsonutil.parse_date(p["date"]),
                close=jsonutil.parse_decimal(p["close"]),
                source=p.get("source", "yahoo"),
            )
        )
    for g in data.get("rsu_grants", []):
        grant = RsuGrant(
            id=g["id"],
            account_id=g["account_id"],
            security_id=g["security_id"],
            grant_date=jsonutil.parse_date(g["grant_date"]),
            note=g.get("note", ""),
        )
        for e in g.get("vesting_events", []):
            grant.vesting_events.append(
                VestingEvent(
                    id=e["id"],
                    vest_date=jsonutil.parse_date(e["vest_date"]),
                    shares=jsonutil.parse_decimal(e["shares"]),
                    released=e.get("released", False),
                    lot_id=e.get("lot_id"),
                )
            )
        db.add(grant)
    for p in data.get("stock_import_profiles", []):
        db.add(
            StockImportProfile(
                id=p["id"], name=p["name"], account_id=p.get("account_id"), config=p.get("config", {})
            )
        )
    if db.get_bind().dialect.name == "postgresql":
        for table in (
            "investment_accounts",
            "securities",
            "lots",
            "stock_transactions",
            "prices",
            "rsu_grants",
            "vesting_events",
            "stock_import_profiles",
        ):
            db.execute(
                text(
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)"
                )
            )
    db.commit()
    return {
        "imported": {
            "accounts": len(data["investment_accounts"]),
            "transactions": len(data["stock_transactions"]),
            "prices": len(data["prices"]),
        }
    }


@router.post("/reset")
def reset(db: Session = Depends(get_db)):
    """Erase every record and come back up as a fresh install. Same table
    sweep as a restore, with nothing loaded afterwards."""
    deleted = {}
    for table in TABLES_DELETE_ORDER:
        deleted[table] = db.execute(text(f"DELETE FROM {table}")).rowcount
    if db.get_bind().dialect.name == "postgresql":
        for table in TABLES_DELETE_ORDER:
            db.execute(
                text(
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)"
                )
            )
    db.commit()
    return {"reset": "stocks", "deleted": deleted}
