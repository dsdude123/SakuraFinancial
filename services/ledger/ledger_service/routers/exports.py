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

from ..db import Base, get_db
from ..models import (
    Account,
    Bill,
    BillOccurrence,
    Category,
    Currency,
    DEFAULT_CURRENCIES,
    FxRate,
    ImportProfile,
    Payee,
    PayeeAlias,
    Split,
    Transaction,
    TransferRule,
)

router = APIRouter(prefix="/api", tags=["export"])

# Deletion order respects FKs (children first); insertion is the reverse.
CORE_TABLES = [
    "bill_occurrences",
    "bills",
    "import_rows",
    "import_batches",
    "import_profiles",
    "transfer_rules",
    "transaction_splits",
    "transactions",
    "payee_aliases",
    "payees",
    "categories",
    "fx_rates",
    "accounts",
    "currencies",
]


def tables_with_serial_id(tables: list[str]) -> list[str]:
    """Of ``tables``, the ones that actually have an ``id`` sequence to reset.

    Not every table is keyed by a serial: ``currencies`` is keyed by its ISO
    code and has no ``id`` at all, so asking Postgres to setval its sequence
    fails the whole request. The model metadata is the authority on which
    tables have one, which lets callers hand over an entire deletion list
    without curating it by hand."""
    return [
        table
        for table in tables
        if "id" in getattr(Base.metadata.tables.get(table), "columns", ())
    ]


def reset_sequences(db: Session, tables: list[str]) -> None:
    if db.get_bind().dialect.name != "postgresql":
        return
    for table in tables_with_serial_id(tables):
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
        "bills": [
            {
                "id": b.id,
                "name": b.name,
                "payee_id": b.payee_id,
                "category_id": b.category_id,
                "account_id": b.account_id,
                "frequency": b.frequency,
                "amount": money_str(b.amount),
                "is_variable": b.is_variable,
                "next_due": b.next_due.isoformat(),
                "active": b.active,
                "note": b.note,
            }
            for b in db.execute(select(Bill).order_by(Bill.id)).scalars()
        ],
        "bill_occurrences": [
            {
                "id": o.id,
                "bill_id": o.bill_id,
                "due_date": o.due_date.isoformat(),
                "expected_amount": money_str(o.expected_amount),
                "actual_amount": money_str(o.actual_amount),
                "status": o.status,
                "matched_transaction_id": o.matched_transaction_id,
            }
            for o in db.execute(select(BillOccurrence).order_by(BillOccurrence.id)).scalars()
        ],
        "transfer_rules": [
            {
                "id": r.id,
                "pattern": r.pattern,
                "match_type": r.match_type,
                "account_id": r.account_id,
                "active": r.active,
                "match_days": r.match_days,
            }
            for r in db.execute(select(TransferRule).order_by(TransferRule.id)).scalars()
        ],
        "import_profiles": [
            {
                "id": p.id,
                "name": p.name,
                "account_id": p.account_id,
                "config": p.config,
            }
            for p in db.execute(select(ImportProfile).order_by(ImportProfile.id)).scalars()
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
    for r in data.get("transfer_rules", []):
        db.add(
            TransferRule(
                id=r["id"],
                pattern=r["pattern"],
                match_type=r.get("match_type", "prefix"),
                account_id=r["account_id"],
                active=r.get("active", True),
                match_days=r.get("match_days", 5),
            )
        )
    for p in data.get("import_profiles", []):
        db.add(
            ImportProfile(
                id=p["id"],
                name=p["name"],
                account_id=p.get("account_id"),
                config=p.get("config", {}),
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
    for b in data.get("bills", []):
        db.add(
            Bill(
                id=b["id"],
                name=b["name"],
                payee_id=b["payee_id"],
                category_id=b.get("category_id"),
                account_id=b.get("account_id"),
                frequency=b.get("frequency", "monthly"),
                amount=jsonutil.parse_decimal(b["amount"]),
                is_variable=b.get("is_variable", False),
                next_due=jsonutil.parse_date(b["next_due"]),
                active=b.get("active", True),
                note=b.get("note", ""),
            )
        )
    for o in data.get("bill_occurrences", []):
        db.add(
            BillOccurrence(
                id=o["id"],
                bill_id=o["bill_id"],
                due_date=jsonutil.parse_date(o["due_date"]),
                expected_amount=jsonutil.parse_decimal(o["expected_amount"]),
                actual_amount=jsonutil.parse_decimal(o.get("actual_amount")),
                status=o.get("status", "upcoming"),
                matched_transaction_id=o.get("matched_transaction_id"),
            )
        )
    reset_sequences(
        db,
        [
            "accounts",
            "categories",
            "payees",
            "payee_aliases",
            "fx_rates",
            "transactions",
            "transaction_splits",
            "bills",
            "bill_occurrences",
            "transfer_rules",
            "import_profiles",
        ],
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
    # Belt and braces: import_core sets the sequences inside the load, so if
    # that load had failed the setvals would have survived the rollback. Doing
    # it again after the commit makes the counters match what is actually
    # stored, whatever happened on the way here.
    reset_sequences(db, CORE_TABLES)
    db.commit()
    return {"imported": counts}


@router.post("/reset")
def reset(db: Session = Depends(get_db)):
    """Erase every record and come back up as a fresh install.

    Same table sweep as a restore, but nothing is loaded afterwards — except
    the default currencies, without which no account can be created and the
    "fresh install" the user expects wouldn't actually be usable."""
    deleted = {}
    for table in CORE_TABLES:
        deleted[table] = db.execute(text(f"DELETE FROM {table}")).rowcount
    for code, name, decimals in DEFAULT_CURRENCIES:
        db.add(Currency(code=code, name=name, decimals=decimals))
    db.commit()
    # Sequences are rewound only once the rows are definitely gone. setval is
    # NOT transactional: doing it first and then failing would roll the deletes
    # back while leaving the counters at 1, and the next insert would collide
    # with rows that still exist.
    reset_sequences(db, CORE_TABLES)
    db.commit()
    return {"reset": "ledger", "deleted": deleted}
