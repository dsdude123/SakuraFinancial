"""Whole-database export/import for backup and disaster recovery.

The export is human-readable JSON keyed by table name; transactions embed
their splits. Import wipes and reloads everything with original IDs preserved
(cross-service links like receipts -> transaction ids survive a restore).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session, joinedload

from sakura_common import jsonutil
from sakura_common.money import money_str

from ..db import get_db
from ..models import (
    Account,
    Category,
    Currency,
    FxRate,
    Payee,
    PayeeAlias,
    Split,
    Transaction,
)

router = APIRouter(prefix="/api", tags=["export"])

# Deletion order respects FKs (children first); insertion is the reverse.
CORE_TABLES = [
    "transaction_splits",
    "transactions",
    "payee_aliases",
    "payees",
    "categories",
    "fx_rates",
    "accounts",
    "currencies",
]


def reset_sequences(db: Session, tables: list[str]) -> None:
    if db.get_bind().dialect.name != "postgresql":
        return
    for table in tables:
        db.execute(
            text(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)"
            )
        )


def export_core(db: Session) -> dict:
    transactions = db.execute(
        select(Transaction).options(joinedload(Transaction.splits)).order_by(Transaction.id)
    ).unique().scalars().all()
    return {
        "currencies": [
            {"code": c.code, "name": c.name, "decimals": c.decimals}
            for c in db.execute(select(Currency).order_by(Currency.code)).scalars()
        ],
        "fx_rates": [
            {
                "id": r.id,
                "date": r.date.isoformat(),
                "from_code": r.from_code,
                "to_code": r.to_code,
                "rate": money_str(r.rate),
            }
            for r in db.execute(select(FxRate).order_by(FxRate.id)).scalars()
        ],
        "accounts": [
            {
                "id": a.id,
                "name": a.name,
                "type": a.type,
                "currency_code": a.currency_code,
                "opening_balance": money_str(a.opening_balance),
                "active": a.active,
                "note": a.note,
            }
            for a in db.execute(select(Account).order_by(Account.id)).scalars()
        ],
        "categories": [
            {
                "id": c.id,
                "name": c.name,
                "parent_id": c.parent_id,
                "kind": c.kind,
                "active": c.active,
            }
            for c in db.execute(select(Category).order_by(Category.id)).scalars()
        ],
        "payees": [
            {
                "id": p.id,
                "name": p.name,
                "default_category_id": p.default_category_id,
                "active": p.active,
            }
            for p in db.execute(select(Payee).order_by(Payee.id)).scalars()
        ],
        "payee_aliases": [
            {
                "id": a.id,
                "payee_id": a.payee_id,
                "pattern": a.pattern,
                "match_type": a.match_type,
            }
            for a in db.execute(select(PayeeAlias).order_by(PayeeAlias.id)).scalars()
        ],
        "transactions": [
            {
                "id": t.id,
                "account_id": t.account_id,
                "date": t.date.isoformat(),
                "payee_id": t.payee_id,
                "memo": t.memo,
                "status": t.status,
                "kind": t.kind,
                "transfer_group_id": t.transfer_group_id,
                "import_hash": t.import_hash,
                "splits": [
                    {
                        "category_id": s.category_id,
                        "amount": money_str(s.amount),
                        "memo": s.memo,
                    }
                    for s in t.splits
                ],
            }
            for t in transactions
        ],
    }


def import_core(db: Session, data: dict) -> dict:
    for key in ("currencies", "accounts", "categories", "payees", "transactions"):
        if not isinstance(data.get(key), list):
            raise HTTPException(422, f"export data missing list {key!r}")
    for table in CORE_TABLES:
        db.execute(text(f"DELETE FROM {table}"))
    for c in data["currencies"]:
        db.add(Currency(code=c["code"], name=c.get("name", c["code"]), decimals=c.get("decimals", 2)))
    for a in data["accounts"]:
        db.add(
            Account(
                id=a["id"],
                name=a["name"],
                type=a["type"],
                currency_code=a.get("currency_code", "USD"),
                opening_balance=jsonutil.parse_decimal(a.get("opening_balance", "0")),
                active=a.get("active", True),
                note=a.get("note", ""),
            )
        )
    for c in data["categories"]:
        db.add(
            Category(
                id=c["id"],
                name=c["name"],
                parent_id=c.get("parent_id"),
                kind=c.get("kind", "expense"),
                active=c.get("active", True),
            )
        )
    for p in data["payees"]:
        db.add(
            Payee(
                id=p["id"],
                name=p["name"],
                default_category_id=p.get("default_category_id"),
                active=p.get("active", True),
            )
        )
    for r in data.get("fx_rates", []):
        db.add(
            FxRate(
                id=r["id"],
                date=jsonutil.parse_date(r["date"]),
                from_code=r["from_code"],
                to_code=r["to_code"],
                rate=jsonutil.parse_decimal(r["rate"]),
            )
        )
    for a in data.get("payee_aliases", []):
        db.add(
            PayeeAlias(
                id=a["id"],
                payee_id=a["payee_id"],
                pattern=a["pattern"],
                match_type=a.get("match_type", "exact"),
            )
        )
    counts = {"transactions": 0}
    for t in data["transactions"]:
        txn = Transaction(
            id=t["id"],
            account_id=t["account_id"],
            date=jsonutil.parse_date(t["date"]),
            payee_id=t.get("payee_id"),
            memo=t.get("memo", ""),
            status=t.get("status", "uncleared"),
            kind=t.get("kind", "normal"),
            transfer_group_id=t.get("transfer_group_id"),
            import_hash=t.get("import_hash"),
        )
        for s in t.get("splits", []):
            txn.splits.append(
                Split(
                    category_id=s.get("category_id"),
                    amount=jsonutil.parse_decimal(s["amount"]),
                    memo=s.get("memo", ""),
                )
            )
        db.add(txn)
        counts["transactions"] += 1
    reset_sequences(
        db, ["accounts", "categories", "payees", "payee_aliases", "fx_rates", "transactions", "transaction_splits"]
    )
    return counts


@router.get("/export")
def export(db: Session = Depends(get_db)):
    data = export_core(db)
    data["service"] = "ledger"
    return data


@router.post("/import")
def import_(data: dict, db: Session = Depends(get_db)):
    counts = import_core(db, data)
    db.commit()
    return {"imported": counts}
