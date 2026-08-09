"""CSV import pipeline.

Flow (mirrors the wizard in the web UI):

1. **preview** — parse + validate the whole file via sakura_common.csvengine.
   Any row error aborts with the full error report; NOTHING is written.
   A valid file becomes an ImportBatch of ImportRows, each classified:
     - ``duplicate``   row hash already imported (or repeated in this file)
     - ``transfer``    a transfer rule matched the description
     - ``ready``       a payee alias matched (payee/category prefilled)
     - ``needs_payee`` unknown description — user picks/creates a payee
2. **review** — the user resolves rows; choosing a payee for a
   ``needs_payee`` row records the description as an alias (learn_alias) so
   next month it maps automatically.
3. **commit** — included rows become transactions (transfers become paired
   legs), aliases are learned, and every new transaction runs through bill
   matching — a fixed bill matched at a different amount comes back in the
   summary so the UI can ask "update the bill?".
"""

from __future__ import annotations

import logging
import os

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from sakura_common.csvengine import ParsedBankRow
from sakura_common.dedup import normalize_description, row_hash
from sakura_common.money import money_str

from ..models import (
    Account,
    ImportBatch,
    ImportRow,
    PayeeAlias,
    Transaction,
    TransferRule,
)
from .bills import match_transaction_to_bills
from .transactions import create_transaction, create_transfer

logger = logging.getLogger(__name__)


def find_alias_payee(db: Session, description: str) -> PayeeAlias | None:
    """Exact match wins, then prefix, then contains."""
    normalized = normalize_description(description)
    exact = db.execute(
        select(PayeeAlias).where(
            PayeeAlias.pattern == normalized, PayeeAlias.match_type == "exact"
        )
    ).scalar_one_or_none()
    if exact is not None:
        return exact
    for alias in db.execute(
        select(PayeeAlias).where(PayeeAlias.match_type == "prefix")
    ).scalars():
        if normalized.startswith(alias.pattern):
            return alias
    for alias in db.execute(
        select(PayeeAlias).where(PayeeAlias.match_type == "contains")
    ).scalars():
        if alias.pattern in normalized:
            return alias
    return None


def find_transfer_rule(db: Session, description: str, account_id: int) -> TransferRule | None:
    normalized = normalize_description(description)
    for rule in db.execute(select(TransferRule).where(TransferRule.active)).scalars():
        if rule.account_id == account_id:
            continue  # a rule can't transfer an account into itself
        pattern = normalize_description(rule.pattern)
        if rule.match_type == "prefix" and normalized.startswith(pattern):
            return rule
        if rule.match_type == "contains" and pattern in normalized:
            return rule
    return None


def build_batch(
    db: Session,
    *,
    account: Account,
    profile_id: int,
    filename: str,
    parsed_rows: list[ParsedBankRow],
) -> ImportBatch:
    """Classify fully-validated rows into a review batch. No transactions yet."""
    batch = ImportBatch(account_id=account.id, profile_id=profile_id, filename=filename)
    seen_hashes: set[str] = set()
    for parsed in parsed_rows:
        digest = row_hash(account.id, parsed.date, parsed.amount, parsed.description)
        already_imported = (
            db.execute(
                select(Transaction.id).where(Transaction.import_hash == digest).limit(1)
            ).scalar_one_or_none()
            is not None
        )
        row = ImportRow(
            line_no=parsed.line_no,
            date=parsed.date,
            description=parsed.description,
            memo=parsed.memo,
            amount=parsed.amount,
            row_hash=digest,
        )
        if already_imported or digest in seen_hashes:
            row.status = "duplicate"
            row.include = False
        else:
            rule = find_transfer_rule(db, parsed.description, account.id)
            if rule is not None:
                row.status = "transfer"
                row.transfer_account_id = rule.account_id
            else:
                alias = find_alias_payee(db, parsed.description)
                if alias is not None:
                    row.status = "ready"
                    row.payee_id = alias.payee_id
                    row.category_id = alias.payee.default_category_id
                else:
                    row.status = "needs_payee"
        seen_hashes.add(digest)
        batch.rows.append(row)
    db.add(batch)
    return batch


def commit_batch(db: Session, batch: ImportBatch) -> dict:
    """Turn included rows into transactions. Runs inside one DB transaction —
    the router commits after this returns, so a failure writes nothing."""
    accounts = {a.id: a for a in db.execute(select(Account)).scalars()}
    account = accounts[batch.account_id]
    created = transfers = skipped = uncategorized = 0
    bill_matches: list[dict] = []
    for row in batch.rows:
        if not row.include or row.transaction_id is not None:
            skipped += 1
            continue
        if row.status == "transfer" and row.transfer_account_id:
            other = accounts[row.transfer_account_id]
            if row.amount < 0:
                leg_out, _ = create_transfer(
                    db,
                    from_account=account,
                    to_account=other,
                    date=row.date,
                    amount=-row.amount,
                    memo=row.description,
                    import_hash=row.row_hash,
                )
                txn = leg_out
            else:
                _, leg_in = create_transfer(
                    db,
                    from_account=other,
                    to_account=account,
                    date=row.date,
                    amount=row.amount,
                    memo=row.description,
                )
                txn = leg_in
                txn.import_hash = row.row_hash
            transfers += 1
        else:
            category_id = row.category_id
            if category_id is None and row.payee is not None:
                category_id = row.payee.default_category_id
            if category_id is None:
                uncategorized += 1
            txn = create_transaction(
                db,
                account=account,
                date=row.date,
                payee_id=row.payee_id,
                memo=row.memo or row.description,
                status="cleared",
                import_hash=row.row_hash,
                splits=[{"category_id": category_id, "amount": row.amount}],
            )
            created += 1
            if row.learn_alias and row.payee_id is not None:
                learn_exact_alias(db, row.payee_id, row.description)
        db.flush()  # assign txn.id so rows/bills can reference it
        row.transaction_id = txn.id
        if txn.kind == "normal":
            bill_matches.extend(match_transaction_to_bills(db, txn))
    batch.status = "committed"
    return {
        "batch_id": batch.id,
        "created": created,
        "transfers": transfers,
        "skipped": skipped,
        "uncategorized": uncategorized,
        "bill_matches": bill_matches,
        "amount_review": [m for m in bill_matches if m.get("status") == "amount_review"],
    }


def learn_exact_alias(db: Session, payee_id: int, description: str) -> None:
    pattern = normalize_description(description)
    existing = db.execute(
        select(PayeeAlias).where(PayeeAlias.pattern == pattern, PayeeAlias.match_type == "exact")
    ).scalar_one_or_none()
    if existing is None:
        db.add(PayeeAlias(payee_id=payee_id, pattern=pattern, match_type="exact"))
    else:
        existing.payee_id = payee_id


def ping_receipts_service() -> None:
    """Best-effort nudge so unlinked receipts get matched against the freshly
    imported transactions. Never allowed to fail an import."""
    base_url = os.environ.get("RECEIPTS_URL")
    if not base_url:
        return
    try:
        httpx.post(f"{base_url.rstrip('/')}/api/match/scan", timeout=5.0)
    except httpx.HTTPError as exc:
        logger.warning("receipts match-scan ping failed: %s", exc)


def row_dict(row: ImportRow) -> dict:
    return {
        "id": row.id,
        "line_no": row.line_no,
        "date": row.date.isoformat(),
        "description": row.description,
        "memo": row.memo,
        "amount": money_str(row.amount),
        "status": row.status,
        "include": row.include,
        "payee_id": row.payee_id,
        "payee_name": row.payee.name if row.payee else None,
        "category_id": row.category_id,
        "transfer_account_id": row.transfer_account_id,
        "learn_alias": row.learn_alias,
        "transaction_id": row.transaction_id,
    }


def batch_dict(batch: ImportBatch, with_rows: bool = True) -> dict:
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
