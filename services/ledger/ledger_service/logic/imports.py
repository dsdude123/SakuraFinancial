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
from decimal import Decimal

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sakura_common.csvengine import ParsedBankRow
from sakura_common.dedup import normalize_description, row_hash
from sakura_common.money import money_str

from ..models import (
    CATEGORY_KINDS,
    Account,
    Category,
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
    """Classify fully-validated rows into a review batch. No transactions yet.

    A row is a duplicate only when *this account has already imported it* — the
    file is never compared against itself. Two coffees from the same shop for
    the same amount on the same day are two real transactions, and both must
    land; the check exists to catch re-importing a statement, not to collapse
    genuine repeats. A hash already imported N times therefore marks only the
    first N matching rows in this file, leaving any extras as new activity."""
    batch = ImportBatch(account_id=account.id, profile_id=profile_id, filename=filename)
    remaining_imported: dict[str, int] = {}
    for parsed in parsed_rows:
        digest = row_hash(account.id, parsed.date, parsed.amount, parsed.description)
        if digest not in remaining_imported:
            remaining_imported[digest] = db.execute(
                select(func.count())
                .select_from(Transaction)
                .where(Transaction.import_hash == digest)
            ).scalar_one()
        already_imported = remaining_imported[digest] > 0
        if already_imported:
            remaining_imported[digest] -= 1
        row = ImportRow(
            line_no=parsed.line_no,
            date=parsed.date,
            description=parsed.description,
            memo=parsed.memo,
            amount=parsed.amount,
            row_hash=digest,
        )
        if already_imported:
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


def resolve_or_create_category(db: Session, path: str, kind: str = "expense") -> Category:
    """Find (or create) a category from a display path.

    Accepts either a bare name ("Groceries") or the "Parent: Child" form the
    pickers render, so a category invented during import can be filed under an
    existing parent without leaving the review screen. Categories nest one level
    only, matching the rest of the app."""
    parts = [part.strip() for part in path.split(":", 1)]
    parts = [part for part in parts if part]
    if not parts:
        raise ValueError("category name is required")
    if kind not in CATEGORY_KINDS:
        raise ValueError(f"kind must be one of {CATEGORY_KINDS}")

    def get_or_add(name: str, parent_id: int | None) -> Category:
        existing = db.execute(
            select(Category).where(Category.name == name, Category.parent_id == parent_id)
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        category = Category(name=name, kind=kind, parent_id=parent_id)
        db.add(category)
        db.flush()
        return category

    if len(parts) == 1:
        return get_or_add(parts[0], None)
    parent = get_or_add(parts[0], None)
    if parent.parent_id is not None:
        raise ValueError("categories can nest only one level deep")
    return get_or_add(parts[1], parent.id)


def description_groups(batch: ImportBatch) -> list[dict]:
    """The review screen's work list: one entry per *distinct* description.

    A statement with fifty unknown rows usually has only a handful of distinct
    merchants. Grouping by normalized description lets the user answer once per
    merchant instead of once per row."""
    groups: dict[str, dict] = {}
    for row in batch.rows:
        key = normalize_description(row.description)
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "key": key,
                "description": row.description,
                "row_ids": [],
                "count": 0,
                "total": Decimal("0"),
                "statuses": set(),
                "payee_id": row.payee_id,
                "category_id": row.category_id,
            }
        group["row_ids"].append(row.id)
        group["count"] += 1
        group["total"] += row.amount
        group["statuses"].add(row.status)
        # A group is "answered" only when every row in it agrees.
        if row.payee_id != group["payee_id"]:
            group["payee_id"] = None
        if row.category_id != group["category_id"]:
            group["category_id"] = None
    result = []
    for group in groups.values():
        statuses = group.pop("statuses")
        result.append(
            {
                **group,
                "total": money_str(group["total"]),
                "needs_payee": "needs_payee" in statuses,
                "status": "needs_payee" if "needs_payee" in statuses else sorted(statuses)[0],
            }
        )
    # Rows still needing an answer float to the top; then most-repeated first.
    result.sort(key=lambda g: (not g["needs_payee"], -g["count"], g["description"]))
    return result


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
        data["description_groups"] = description_groups(batch)
    return data
