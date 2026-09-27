"""Stock CSV import: same all-or-nothing pipeline as bank imports, plus the
per-profile action map (broker strings -> internal actions). Parsing failures
— including an unmapped action string — abort before anything is stored.

Cash rows get one extra check the bank importer also does: a deposit or
withdrawal that is really the far side of a bank transfer already recorded here
(the bank statement was imported first, and that import booked the money into
this account) is flagged ``counterpart`` and left out, instead of crediting the
same money twice. The two statements rarely agree on the date, so the match has
a window — ``transfer_match_days`` in the profile config, 5 days by default.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sakura_common.csvengine import ParsedStockRow
from sakura_common.money import money_str, qty_str

from ..models import (
    InvestmentAccount,
    Lot,
    Security,
    StockImportBatch,
    StockImportRow,
    StockTransaction,
)
from . import portfolio

ZERO = Decimal("0")


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


DEFAULT_TRANSFER_MATCH_DAYS = 5


def find_transfer_counterpart(
    db: Session,
    *,
    account: InvestmentAccount,
    action: str,
    amount: Decimal,
    when,
    window_days: int,
    claimed: set[int],
) -> StockTransaction | None:
    """The linked cash row this CSV row *is*, if there is one.

    When a bank statement is imported first, its transfer rule books the money
    into this account already — so the broker's own "wire in" line for the same
    movement would credit it twice. Row hashes can't catch that: the row was
    never imported here, it was created by the other side. What the two agree on
    is the amount and roughly the date, the same match the bank importer makes
    against an already-recorded transfer leg.

    Only rows carrying ``external_account`` count: those are halves of a bank
    transfer. An ordinary deposit someone typed in is left alone, because
    nothing says it is this row. ``claimed`` stops two identical lines in one
    file from both matching the same transaction.
    """
    if action not in ("deposit", "withdraw") or amount is None:
        return None
    if window_days < 0:
        window_days = 0
    signed = abs(amount) if action == "deposit" else -abs(amount)
    candidates = db.execute(
        select(StockTransaction).where(
            StockTransaction.account_id == account.id,
            StockTransaction.type == action,
            StockTransaction.external_account.is_not(None),
            StockTransaction.date >= when - timedelta(days=window_days),
            StockTransaction.date <= when + timedelta(days=window_days),
        )
    ).scalars().all()
    for txn in sorted(candidates, key=lambda t: (abs((t.date - when).days), t.id)):
        if txn.id not in claimed and txn.amount == signed:
            return txn
    return None


def held_quantities(db: Session, account: InvestmentAccount) -> dict[str, Decimal]:
    """Shares currently held per symbol, as FIFO sees them."""
    rows = db.execute(
        select(Security.symbol, func.sum(Lot.quantity))
        .join(Security, Security.id == Lot.security_id)
        .where(Lot.account_id == account.id)
        .group_by(Security.symbol)
    ).all()
    return {symbol.upper(): Decimal(str(quantity or 0)) for symbol, quantity in rows}


def build_batch(
    db: Session,
    *,
    account: InvestmentAccount,
    profile_id: int,
    filename: str,
    parsed_rows: list[ParsedStockRow],
    config: dict | None = None,
) -> StockImportBatch:
    """Classify parsed rows for review.

    Duplicate detection compares the file against what is **already in the
    account**, never the file against itself: buying the same lot twice in one
    day is ordinary, and those rows must both import. A hash seen N times in the
    account marks the first N matching rows in this file as already-imported;
    anything beyond that is new activity.

    Rows are also checked against the account's holdings: a broker export that
    starts mid-history often sells a position that was bought before the import
    window, and FIFO has no lot to draw from. Those rows are marked ``no_lots``
    and left out of the import rather than failing the whole commit — the user
    records the opening position, then re-includes the row.

    Cash rows are checked against transfers already booked from the bank side,
    so importing both statements of one movement doesn't credit it twice; those
    are marked ``counterpart``."""
    batch = StockImportBatch(account_id=account.id, profile_id=profile_id, filename=filename)
    remaining_imported: dict[str, int] = {}
    held = held_quantities(db, account)
    window_days = int((config or {}).get("transfer_match_days", DEFAULT_TRANSFER_MATCH_DAYS))
    claimed_transfers: set[int] = set()
    # Same order commit_batch uses, so the simulation matches what FIFO will see.
    for parsed in sorted(parsed_rows, key=lambda r: (r.date, r.line_no)):
        digest = row_hash(account.id, parsed)
        if digest not in remaining_imported:
            remaining_imported[digest] = db.execute(
                select(func.count())
                .select_from(StockTransaction)
                .where(StockTransaction.import_hash == digest)
            ).scalar_one()
        duplicate = remaining_imported[digest] > 0
        if duplicate:
            remaining_imported[digest] -= 1

        status = "duplicate" if duplicate else "ready"
        if not duplicate:
            counterpart = find_transfer_counterpart(
                db,
                account=account,
                action=parsed.action,
                amount=parsed.amount,
                when=parsed.date,
                window_days=window_days,
                claimed=claimed_transfers,
            )
            if counterpart is not None:
                claimed_transfers.add(counterpart.id)
                status = "counterpart"
        if status == "ready" and parsed.symbol and parsed.quantity:
            symbol = parsed.symbol.upper()
            if parsed.action in ("buy", "vest"):
                held[symbol] = held.get(symbol, ZERO) + parsed.quantity
            elif parsed.action == "sell":
                if parsed.quantity > held.get(symbol, ZERO):
                    status = "no_lots"
                else:
                    held[symbol] -= parsed.quantity

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
                status=status,
                include=status == "ready",
            )
        )
    batch.rows.sort(key=lambda r: r.line_no)
    db.add(batch)
    return batch


def normalize_amount(action: str, amount):
    """Put a broker's amount into the sign convention apply_transaction wants.

    Sign conventions vary by broker and even by column within one export — some
    write a purchase as a positive cost, others as a negative cash movement. The
    action already says which way money went, so the magnitude is what matters:
    buys are negative (cash out), everything else is a positive magnitude that
    apply_transaction signs itself."""
    if amount is None:
        return None
    if action == "buy":
        return -abs(amount)
    if action in ("sell", "dividend", "deposit", "withdraw", "fee"):
        return abs(amount)
    return amount


def commit_batch(db: Session, batch: StockImportBatch) -> dict:
    account = db.get(InvestmentAccount, batch.account_id)
    created = skipped = 0
    new_symbols: set[str] = set()
    # Oldest first so FIFO selling sees buys before sells within one file.
    for row in sorted(batch.rows, key=lambda r: (r.date, r.line_no)):
        if not row.include or row.transaction_id is not None:
            skipped += 1
            continue
        amount = normalize_amount(row.action, row.amount)
        try:
            txn = portfolio.apply_transaction(
                db,
                account=account,
                type=row.action,
                date=row.date,
                symbol=row.symbol,
                quantity=row.quantity,
                price=row.price,
                amount=amount,
                fees=abs(row.fee) if row.fee else 0,
                note=row.description,
                import_hash=row.row_hash,
            )
        except HTTPException as exc:
            # Point at the CSV line, not just the rule that tripped — the user
            # is looking at a file, not at our transaction model.
            raise HTTPException(
                exc.status_code,
                f"line {row.line_no} ({row.action} {row.symbol or 'cash'} "
                f"on {row.date.isoformat()}): {exc.detail} - uncheck that row or "
                f"fix the file, then import again; nothing was imported",
            ) from exc
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
