"""Bill scheduling, matching, and accrual.

What MS Money got wrong, fixed here:

- **Total bill load is always visible.** `accrual_summary` reduces every bill
  to a *monthly load* (an annual $1,200 insurance premium is $100/month) and
  groups it by category — so the bills overview can show what the month
  really costs, including the bills that only strike once a year.
- **Annual bills accrue.** Each non-monthly bill reports how much should be
  set aside *so far* in its cycle, so a December premium stops being a
  December surprise.
- **Matching includes imports.** Any new normal transaction — hand-entered or
  CSV-imported — is offered to open occurrences. A fixed-amount bill that
  matches at a different amount flags ``amount_review`` and the UI asks:
  update the bill to the new amount, or keep it?
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select
from sqlalchemy.orm import Session

from sakura_common.money import money_str

from ..models import Bill, BillOccurrence, Transaction
from ..serialize import category_path

# How far ahead occurrences are materialized, and how far a transaction date
# may sit from a due date and still match.
GENERATION_HORIZON_DAYS = 400
MATCH_WINDOW_DAYS = 10

FREQUENCIES = ("weekly", "biweekly", "monthly", "quarterly", "semiannual", "annual")

# Monthly accrual factor per frequency (Decimal-safe).
_MONTHLY_FACTOR = {
    "weekly": Decimal(52) / Decimal(12),
    "biweekly": Decimal(26) / Decimal(12),
    "monthly": Decimal(1),
    "quarterly": Decimal(1) / Decimal(3),
    "semiannual": Decimal(1) / Decimal(6),
    "annual": Decimal(1) / Decimal(12),
}

_CYCLE_MONTHS = {"monthly": 1, "quarterly": 3, "semiannual": 6, "annual": 12}


def add_months(when: date, months: int) -> date:
    year = when.year + (when.month - 1 + months) // 12
    month = (when.month - 1 + months) % 12 + 1
    day = min(when.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def next_due_after(frequency: str, due: date) -> date:
    if frequency == "weekly":
        return due + timedelta(days=7)
    if frequency == "biweekly":
        return due + timedelta(days=14)
    return add_months(due, _CYCLE_MONTHS[frequency])


def monthly_load(bill: Bill) -> Decimal:
    load = bill.amount * _MONTHLY_FACTOR[bill.frequency]
    return load.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def ensure_occurrences(db: Session, bill: Bill, today: date | None = None) -> None:
    """Materialize occurrences from the bill's anchor date through the
    horizon. Idempotent: existing (bill, due_date) rows are kept as-is."""
    if not bill.active:
        return
    today = today or date.today()
    horizon = today + timedelta(days=GENERATION_HORIZON_DAYS)
    existing = {
        occ.due_date
        for occ in db.execute(
            select(BillOccurrence).where(BillOccurrence.bill_id == bill.id)
        ).scalars()
    }
    due = bill.next_due
    while due <= horizon:
        if due not in existing:
            db.add(
                BillOccurrence(
                    bill_id=bill.id, due_date=due, expected_amount=bill.amount, status="upcoming"
                )
            )
        due = next_due_after(bill.frequency, due)


def occurrence_dict(occ: BillOccurrence) -> dict:
    bill = occ.bill
    return {
        "id": occ.id,
        "bill_id": occ.bill_id,
        "bill_name": bill.name if bill else None,
        "category_id": bill.category_id if bill else None,
        "category_name": category_path(bill.category) if bill else None,
        "payee_id": bill.payee_id if bill else None,
        "is_variable": bill.is_variable if bill else None,
        "due_date": occ.due_date.isoformat(),
        "expected_amount": money_str(occ.expected_amount),
        "actual_amount": money_str(occ.actual_amount),
        "status": occ.status,
        "matched_transaction_id": occ.matched_transaction_id,
    }


def match_transaction_to_bills(db: Session, txn: Transaction) -> list[dict]:
    """Offer a new transaction to open occurrences. Called for every manual
    entry and every imported row. Returns match-result dicts; an
    ``amount_review`` result is the UI's cue to prompt about updating the
    bill's amount."""
    if txn.kind != "normal" or txn.payee_id is None:
        return []
    amount = -txn.total  # bills are outflows; compare as positive
    if amount <= 0:
        return []
    window_start = txn.date - timedelta(days=MATCH_WINDOW_DAYS)
    window_end = txn.date + timedelta(days=MATCH_WINDOW_DAYS)
    candidates = (
        db.execute(
            select(BillOccurrence)
            .join(Bill, BillOccurrence.bill_id == Bill.id)
            .where(
                Bill.active,
                Bill.payee_id == txn.payee_id,
                BillOccurrence.status == "upcoming",
                BillOccurrence.due_date >= window_start,
                BillOccurrence.due_date <= window_end,
            )
        )
        .scalars()
        .all()
    )
    if not candidates:
        return []
    # Closest due date wins; one transaction settles one occurrence.
    best = min(candidates, key=lambda occ: abs((occ.due_date - txn.date).days))
    best.matched_transaction_id = txn.id
    best.actual_amount = amount
    if best.bill.is_variable or amount == best.expected_amount:
        best.status = "paid"
    else:
        best.status = "amount_review"
    ensure_occurrences(db, best.bill)
    return [occurrence_dict(best)]


def resolve_amount_review(db: Session, occ: BillOccurrence, action: str) -> dict:
    """User answered the "bill amount changed" prompt.

    - ``update_bill``: adopt the new amount on the bill and all its future
      upcoming occurrences.
    - ``keep``: one-off deviation; the bill keeps its amount.
    """
    if occ.status != "amount_review":
        raise ValueError(f"occurrence is {occ.status}, not amount_review")
    if action == "update_bill":
        occ.bill.amount = occ.actual_amount
        for future in db.execute(
            select(BillOccurrence).where(
                BillOccurrence.bill_id == occ.bill_id,
                BillOccurrence.status == "upcoming",
                BillOccurrence.due_date > occ.due_date,
            )
        ).scalars():
            future.expected_amount = occ.actual_amount
    elif action != "keep":
        raise ValueError("action must be 'update_bill' or 'keep'")
    occ.expected_amount = occ.actual_amount if action == "update_bill" else occ.expected_amount
    occ.status = "paid"
    return occurrence_dict(occ)


def accrual_summary(db: Session, today: date | None = None) -> dict:
    """The bills overview: every active bill's monthly load, set-aside
    progress for non-monthly bills, grouped totals by category."""
    today = today or date.today()
    bills = db.execute(select(Bill).where(Bill.active).order_by(Bill.name)).scalars().all()
    rows = []
    by_category: dict[int | None, dict] = {}
    total = Decimal("0")
    for bill in bills:
        load = monthly_load(bill)
        total += load
        next_upcoming = db.execute(
            select(BillOccurrence)
            .where(BillOccurrence.bill_id == bill.id, BillOccurrence.status == "upcoming")
            .order_by(BillOccurrence.due_date)
            .limit(1)
        ).scalar_one_or_none()
        next_due = next_upcoming.due_date if next_upcoming else bill.next_due
        cycle_months = _CYCLE_MONTHS.get(bill.frequency)
        set_aside_target = None
        months_until_due = None
        if cycle_months and cycle_months > 1:
            months_until_due = max(
                0, (next_due.year - today.year) * 12 + (next_due.month - today.month)
            )
            months_into_cycle = max(0, min(cycle_months, cycle_months - months_until_due))
            set_aside_target = (load * months_into_cycle).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        rows.append(
            {
                "id": bill.id,
                "name": bill.name,
                "category_id": bill.category_id,
                "category_name": category_path(bill.category),
                "frequency": bill.frequency,
                "amount": money_str(bill.amount),
                "is_variable": bill.is_variable,
                "monthly_load": money_str(load),
                "next_due": next_due.isoformat(),
                "months_until_due": months_until_due,
                "set_aside_target": money_str(set_aside_target),
            }
        )
        bucket = by_category.setdefault(
            bill.category_id,
            {
                "category_id": bill.category_id,
                "category_name": category_path(bill.category),
                "monthly_load": Decimal("0"),
            },
        )
        bucket["monthly_load"] += load
    return {
        "bills": rows,
        "total_monthly_load": money_str(total),
        "by_category": [
            {**bucket, "monthly_load": money_str(bucket["monthly_load"])}
            for bucket in sorted(
                by_category.values(), key=lambda b: b["monthly_load"], reverse=True
            )
        ],
    }
