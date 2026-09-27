"""Transaction domain logic: creation, transfers, valuations, split editing.

These functions add objects to the session but do not commit — the router
owns the commit so multi-step operations (e.g. import commit + bill matching)
stay atomic.
"""

from __future__ import annotations

import uuid
from datetime import date as date_type
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..models import Account, Split, Transaction


def create_transaction(
    db: Session,
    *,
    account: Account,
    date: date_type,
    splits: list[dict],
    payee_id: int | None = None,
    memo: str = "",
    status: str = "uncleared",
    kind: str = "normal",
    transfer_group_id: str | None = None,
    external_account: str | None = None,
    import_hash: str | None = None,
) -> Transaction:
    if not splits:
        raise HTTPException(422, "a transaction needs at least one split")
    txn = Transaction(
        account_id=account.id,
        date=date,
        payee_id=payee_id,
        memo=memo,
        status=status,
        kind=kind,
        transfer_group_id=transfer_group_id,
        external_account=external_account,
        import_hash=import_hash,
    )
    for split in splits:
        txn.splits.append(
            Split(
                category_id=split.get("category_id"),
                amount=Decimal(str(split["amount"])),
                memo=split.get("memo", ""),
            )
        )
    db.add(txn)
    return txn


def create_transfer(
    db: Session,
    *,
    from_account: Account,
    to_account: Account,
    date: date_type,
    amount: Decimal,
    to_amount: Decimal | None = None,
    memo: str = "",
    import_hash: str | None = None,
) -> tuple[Transaction, Transaction]:
    """Two legs, one transfer_group_id, no categories. ``to_amount`` covers
    cross-currency transfers (defaults to ``amount``)."""
    if amount <= 0:
        raise HTTPException(422, "transfer amount must be positive")
    if from_account.id == to_account.id:
        raise HTTPException(422, "cannot transfer an account to itself")
    group = str(uuid.uuid4())
    received = to_amount if to_amount is not None else amount
    leg_out = create_transaction(
        db,
        account=from_account,
        date=date,
        memo=memo or f"Transfer to {to_account.name}",
        kind="transfer",
        transfer_group_id=group,
        import_hash=import_hash,
        splits=[{"category_id": None, "amount": -amount}],
    )
    leg_in = create_transaction(
        db,
        account=to_account,
        date=date,
        memo=memo or f"Transfer from {from_account.name}",
        kind="transfer",
        transfer_group_id=group,
        splits=[{"category_id": None, "amount": received}],
    )
    return leg_out, leg_in


def create_external_transfer(
    db: Session,
    *,
    account: Account,
    date: date_type,
    amount: Decimal,
    external_account: str,
    external_name: str = "",
    direction: str = "out",
    memo: str = "",
    transfer_group_id: str | None = None,
) -> Transaction:
    """One leg of a transfer whose other side is not a ledger account.

    A brokerage or RSU account lives in the stocks service, so there is no
    second row to write here — the money leaves checking (``direction="out"``)
    or arrives from the investment account (``direction="in"``) and the other
    service books the matching cash row under the same ``transfer_group_id``.
    The leg is a categoryless ``kind='transfer'`` like any other, which is
    what keeps it out of spending and cash-flow reports; before this existed
    the only way to record funding a brokerage was to invent an expense
    category for it, which is exactly the pollution transfers avoid.

    The caller owns the other side. It passes the group id when it has
    already written it, and otherwise takes the one on the returned leg.
    """
    if amount <= 0:
        raise HTTPException(422, "transfer amount must be positive")
    if direction not in ("out", "in"):
        raise HTTPException(422, "direction must be 'out' or 'in'")
    if not external_account.strip():
        raise HTTPException(422, "an external transfer needs the other account's ref")
    label = external_name or external_account
    signed = -amount if direction == "out" else amount
    return create_transaction(
        db,
        account=account,
        date=date,
        memo=memo or (f"Transfer to {label}" if direction == "out" else f"Transfer from {label}"),
        kind="transfer",
        transfer_group_id=transfer_group_id or str(uuid.uuid4()),
        external_account=external_account.strip(),
        splits=[{"category_id": None, "amount": signed}],
    )


def set_asset_value(
    db: Session, *, account: Account, date: date_type, new_value: Decimal, current_balance: Decimal
) -> Transaction:
    """Record a valuation change (car depreciated, house appraised...) as a
    kind='valuation' transaction for the delta. Never appears in cash flow."""
    if account.type not in ("asset", "liability"):
        raise HTTPException(422, f"account {account.name!r} is not an asset/liability account")
    delta = new_value - current_balance
    if delta == 0:
        raise HTTPException(422, "value is unchanged")
    return create_transaction(
        db,
        account=account,
        date=date,
        memo=f"Value adjustment to {new_value}",
        kind="valuation",
        splits=[{"category_id": None, "amount": delta}],
    )


def replace_splits(txn: Transaction, new_splits: list[dict]) -> Transaction:
    """Re-divide one transaction's total across categories (receipt splits,
    manual recategorization). The total is invariant: a split can never change
    what the account balance says happened."""
    if txn.kind != "normal":
        raise HTTPException(422, f"cannot edit splits of a {txn.kind} transaction")
    if not new_splits:
        raise HTTPException(422, "a transaction needs at least one split")
    new_total = sum(Decimal(str(split["amount"])) for split in new_splits)
    if new_total != txn.total:
        raise HTTPException(
            422,
            f"split amounts must sum to the transaction total {txn.total} (got {new_total})",
        )
    txn.splits.clear()
    for split in new_splits:
        txn.splits.append(
            Split(
                category_id=split.get("category_id"),
                amount=Decimal(str(split["amount"])),
                memo=split.get("memo", ""),
            )
        )
    return txn
