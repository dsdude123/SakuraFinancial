"""Stock CSV import: same all-or-nothing pipeline as bank imports, plus the
per-profile action map (broker strings -> internal actions). Parsing failures
— including an unmapped action string — abort before anything is stored."""

from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from sakura_common.csvengine import ParsedStockRow
from sakura_common.money import money_str, qty_str

from ..models import (
    InvestmentAccount,
    StockImportBatch,
    StockImportRow,
    StockTransaction,
)
from . import portfolio


def row_hash(account_id: int, parsed: ParsedStockRow) -> str:
    key = "|".join(
        str(part)
        for part in (
            account_id,
            parsed.date.isoformat(),
            parsed.action,
            parsed.symbol,
            parsed.quantity,
            parsed.price,
            parsed.amount,
        )
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def build_batch(
    db: Session,
    *,
    account: InvestmentAccount,
    profile_id: int,
    filename: str,
    parsed_rows: list[ParsedStockRow],
) -> StockImportBatch:
    batch = StockImportBatch(account_id=account.id, profile_id=profile_id, filename=filename)
    seen: set[str] = set()
    for parsed in parsed_rows:
        digest = row_hash(account.id, parsed)
        duplicate = (
            digest in seen
            or db.execute(
                select(StockTransaction.id)
                .where(StockTransaction.import_hash == digest)
                .limit(1)
            ).scalar_one_or_none()
            is not None
        )
        seen.add(digest)
        batch.rows.append(
            StockImportRow(
                line_no=parsed.line_no,
                date=parsed.date,
                action=parsed.action,
                symbol=parsed.symbol,
                quantity=parsed.quantity,
                price=parsed.price,
                fee=parsed.fee,
                amount=parsed.amount,
                description=parsed.description,
                row_hash=digest,
                status="duplicate" if duplicate else "ready",
                include=not duplicate,
            )
        )
    db.add(batch)
    return batch


def commit_batch(db: Session, batch: StockImportBatch) -> dict:
    account = db.get(InvestmentAccount, batch.account_id)
    created = skipped = 0
    new_symbols: set[str] = set()
    # Oldest first so FIFO selling sees buys before sells within one file.
    for row in sorted(batch.rows, key=lambda r: (r.date, r.line_no)):
        if not row.include or row.transaction_id is not None:
            skipped += 1
            continue
        amount = row.amount
        if amount is not None and row.action in ("withdraw", "fee") and amount < 0:
            amount = -amount  # brokers export these negative; API wants magnitude
        if amount is not None and row.action == "buy" and amount > 0:
            amount = -amount  # buy amounts arrive as positive cost in some exports
        txn = portfolio.apply_transaction(
            db,
            account=account,
            type=row.action,
            date=row.date,
            symbol=row.symbol,
            quantity=row.quantity,
            price=row.price,
            amount=amount if amount is not None else None,
            fees=row.fee or 0,
            note=row.description,
            import_hash=row.row_hash,
        )
        row.transaction_id = txn.id
        created += 1
        if row.symbol:
            new_symbols.add(row.symbol.upper())
    batch.status = "committed"
    return {
        "batch_id": batch.id,
        "created": created,
        "skipped": skipped,
        "symbols": sorted(new_symbols),
    }


def row_dict(row: StockImportRow) -> dict:
    return {
        "id": row.id,
        "line_no": row.line_no,
        "date": row.date.isoformat(),
        "action": row.action,
        "symbol": row.symbol,
        "quantity": qty_str(row.quantity),
        "price": money_str(row.price),
        "fee": money_str(row.fee),
        "amount": money_str(row.amount),
        "description": row.description,
        "status": row.status,
        "include": row.include,
        "transaction_id": row.transaction_id,
    }


def batch_dict(batch: StockImportBatch, with_rows: bool = True) -> dict:
    counts: dict[str, int] = {}
    for row in batch.rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    data = {
        "id": batch.id,
        "account_id": batch.account_id,
        "account_name": batch.account.name if batch.account else None,
        "profile_id": batch.profile_id,
        "filename": batch.filename,
        "status": batch.status,
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
        "row_counts": counts,
        "total_rows": len(batch.rows),
    }
    if with_rows:
        data["rows"] = [row_dict(row) for row in batch.rows]
    return data
