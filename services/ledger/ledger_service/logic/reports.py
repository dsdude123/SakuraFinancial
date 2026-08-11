"""Report aggregations.

Ground rules, straight from the requirements:

- Cash-flow style reports only look at ``kind='normal'`` transactions in
  cash-flow account types. Transfers have no category and never appear;
  valuation changes (car losing value) never appear.
- Category amounts are **net**: a reimbursement inflow against Rent reduces
  Rent spending.
- Net worth counts everything, converted to a base currency, with cash and
  non-cash reported separately.
"""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sakura_common.money import money_str

from ..models import (
    Account,
    CASHFLOW_ACCOUNT_TYPES,
    Category,
    Split,
    Transaction,
)
from ..serialize import category_path
from . import fx
from .balances import account_balance

ZERO = Decimal("0")


def month_bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def category_actuals(db: Session, start: date, end: date) -> list[dict]:
    """Net amount per category over [start, end] for normal transactions in
    cash-flow accounts. Uncategorized splits come back with category_id None."""
    rows = db.execute(
        select(Split.category_id, func.sum(Split.amount))
        .join(Transaction, Split.transaction_id == Transaction.id)
        .join(Account, Transaction.account_id == Account.id)
        .where(
            Transaction.kind == "normal",
            Transaction.date >= start,
            Transaction.date <= end,
            Account.type.in_(CASHFLOW_ACCOUNT_TYPES),
        )
        .group_by(Split.category_id)
    ).all()
    categories = {c.id: c for c in db.execute(select(Category)).scalars()}
    result = []
    for category_id, net in rows:
        category = categories.get(category_id)
        result.append(
            {
                "category_id": category_id,
                "name": category.name if category else "(uncategorized)",
                "kind": category.kind if category else ("income" if net > 0 else "expense"),
                "parent_id": category.parent_id if category else None,
                "net": money_str(Decimal(str(net))),
            }
        )
    result.sort(key=lambda row: (row["kind"], row["name"]))
    return result


def category_tree(db: Session, start: date, end: date) -> list[dict]:
    """Category actuals rolled up one level: each parent reports its own
    direct spending plus its children's, with the children nested underneath.

    This is what makes subcategories worth having — "Food -1,050.00" broken
    into Groceries and Dining Out, instead of two unrelated rows.
    """
    flat = {row["category_id"]: row for row in category_actuals(db, start, end)}
    categories = {c.id: c for c in db.execute(select(Category)).scalars()}

    # Every category that has activity, plus the parents of any active child
    # (a parent with no direct spending of its own still needs a total row).
    involved: set[int | None] = set(flat)
    for category_id in list(flat):
        category = categories.get(category_id)
        if category is not None and category.parent_id is not None:
            involved.add(category.parent_id)

    def net_of(category_id) -> Decimal:
        row = flat.get(category_id)
        return Decimal(row["net"]) if row else ZERO

    nodes: list[dict] = []
    for category_id in involved:
        category = categories.get(category_id)
        if category is not None and category.parent_id is not None:
            continue  # children are attached to their parent below
        children = [
            {
                "category_id": child_id,
                "name": categories[child_id].name,
                "path": category_path(categories[child_id]),
                "kind": categories[child_id].kind,
                "net": money_str(net_of(child_id)),
            }
            for child_id in involved
            if child_id is not None
            and categories.get(child_id) is not None
            and categories[child_id].parent_id == category_id
        ]
        children.sort(key=lambda row: row["name"].lower())
        own = net_of(category_id)
        total = own + sum((Decimal(child["net"]) for child in children), ZERO)
        nodes.append(
            {
                "category_id": category_id,
                "name": category.name if category else "(uncategorized)",
                "path": category_path(category),
                "kind": category.kind if category else ("income" if total > 0 else "expense"),
                "own_net": money_str(own),
                "net": money_str(total),
                "children": children,
            }
        )
    nodes.sort(key=lambda row: (row["kind"], row["name"].lower()))
    return nodes


def cashflow_by_month(db: Session, months: int, end: date | None = None) -> list[dict]:
    """Per calendar month: income (net of income categories), spending (net of
    expense categories, returned positive), and net. Uncategorized splits are
    classified by sign so unfinished imports don't vanish from the report."""
    end = end or date.today()
    series = []
    for offset in range(months - 1, -1, -1):
        year, month = shift_month(end.year, end.month, -offset)
        start_day, end_day = month_bounds(year, month)
        income = ZERO
        spending = ZERO
        for row in category_actuals(db, start_day, end_day):
            net = Decimal(row["net"])
            if row["kind"] == "income":
                income += net
            else:
                spending -= net  # expenses are negative nets; flip to positive
        series.append(
            {
                "month": f"{year:04d}-{month:02d}",
                "income": money_str(income),
                "spending": money_str(spending),
                "net": money_str(income - spending),
            }
        )
    return series


def net_worth_series(db: Session, months: int, base_currency: str = "USD") -> list[dict]:
    """Month-end snapshots: cash (checking/savings/cash/credit_card),
    assets, liabilities, total — each converted to the base currency. Accounts
    with no FX rate convert 1:1 and are listed in missing_rates."""
    today = date.today()
    accounts = db.execute(select(Account).where(Account.active)).scalars().all()
    series = []
    for offset in range(months - 1, -1, -1):
        year, month = shift_month(today.year, today.month, -offset)
        _, month_end = month_bounds(year, month)
        buckets = {"cash": ZERO, "assets": ZERO, "liabilities": ZERO}
        missing_rates: list[str] = []
        for account in accounts:
            balance = account_balance(db, account, as_of=month_end)
            converted, found = fx.convert(db, balance, account.currency_code, base_currency, month_end)
            if not found and account.currency_code != base_currency:
                missing_rates.append(account.currency_code)
            if account.type in CASHFLOW_ACCOUNT_TYPES:
                buckets["cash"] += converted
            elif account.type == "asset":
                buckets["assets"] += converted
            else:
                buckets["liabilities"] += converted
        series.append(
            {
                "month": f"{year:04d}-{month:02d}",
                "date": month_end.isoformat(),
                "cash": money_str(buckets["cash"]),
                "assets": money_str(buckets["assets"]),
                "liabilities": money_str(buckets["liabilities"]),
                "total": money_str(buckets["cash"] + buckets["assets"] + buckets["liabilities"]),
                "missing_rates": sorted(set(missing_rates)),
                "base_currency": base_currency,
            }
        )
    return series
