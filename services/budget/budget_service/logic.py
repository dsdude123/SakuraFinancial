"""The budget engine: YNAB's good ideas without its annoyances.

Three rules, straight from the requirements:

1. **Income assigns itself.** Nobody hand-assigns dollars. Each month's income
   flows through a waterfall: (1) cover what's already been spent, (2) set
   aside accruals for upcoming non-monthly bills, (3) fund savings goals,
   (4) the remainder lands in the **General Fund** — unassigned money is a
   feature, never a nag.
2. **Deficits carry, surpluses don't.** Overspend a category and the negative
   carries into next month's available amount until recovered (spend less to
   heal it). Underspend and the leftover goes to the General Fund instead of
   inflating next month's category.
3. **Actuals come from the ledger live** — including reimbursements netting
   against their category and transfers/valuations never appearing at all.

The engine recomputes history from the first budgeted month every time; there
is no stored derived state to get stale.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from sakura_common.money import money_str

from .models import CategoryBudget, Goal

ZERO = Decimal("0")


def parse_month(text: str) -> date:
    try:
        year, month = (int(part) for part in text.split("-"))
        return date(year, month, 1)
    except (ValueError, TypeError):
        raise ValueError(f"month must look like 2026-08, not {text!r}")


def month_end(month_start: date) -> date:
    return date(
        month_start.year,
        month_start.month,
        calendar.monthrange(month_start.year, month_start.month)[1],
    )


def next_month(month_start: date) -> date:
    year = month_start.year + (1 if month_start.month == 12 else 0)
    month = 1 if month_start.month == 12 else month_start.month + 1
    return date(year, month, 1)


@dataclass
class MonthComputation:
    income: Decimal = ZERO
    spent_by_category: dict[int, Decimal] = field(default_factory=dict)
    spent_by_envelope: dict[int, Decimal] = field(default_factory=dict)
    carry_out: dict[int, Decimal] = field(default_factory=dict)
    goal_allocations: dict[int, Decimal] = field(default_factory=dict)
    general_fund_delta: Decimal = ZERO
    bill_set_aside: Decimal = ZERO


def budget_changes(db: Session, through: date | None = None) -> dict[date, dict[int, Decimal]]:
    """Every stored change point, grouped by the month it takes effect."""
    query = select(CategoryBudget).order_by(CategoryBudget.month, CategoryBudget.id)
    if through is not None:
        query = query.where(CategoryBudget.month <= through)
    changes: dict[date, dict[int, Decimal]] = {}
    for row in db.execute(query).scalars():
        changes.setdefault(row.month, {})[row.category_id] = row.amount
    return changes


def resolve_plan(db: Session, month_start: date) -> dict[int, tuple[Decimal, date]]:
    """The budget in force for a month: for each category, the most recent
    change at or before it, with the month that change was made.

    Categories stopped (amount 0) are left out entirely rather than reported
    as a zero budget — an envelope that exists with nothing in it behaves
    differently from one that doesn't exist, and "removed from the budget"
    means the latter."""
    changes = budget_changes(db, through=month_start)
    latest: dict[int, tuple[Decimal, date]] = {}
    for month in sorted(changes):  # oldest first, so later changes win
        for category_id, amount in changes[month].items():
            latest[category_id] = (amount, month)
    return {cid: entry for cid, entry in latest.items() if entry[0] > 0}


def plan_amounts(db: Session, month_start: date) -> dict[int, Decimal]:
    """``resolve_plan`` without the provenance — what the engine budgets with."""
    return {cid: amount for cid, (amount, _month) in resolve_plan(db, month_start).items()}


def envelope_for(category_id: int, budgets: dict, parents: dict[int, int | None]) -> int:
    """Which envelope does spending on this category land in?

    You may budget at either level. Spending checks the category itself first,
    then walks up to its parent: a budget on "Groceries" wins for grocery
    spending, and with no such budget the money lands in "Food". A category
    with no budgeted ancestor is its own (unbudgeted) envelope, so overspend
    still shows up somewhere rather than vanishing.
    """
    node: int | None = category_id
    seen: set[int] = set()
    while node is not None and node not in seen:
        if node in budgets:
            return node
        seen.add(node)
        node = parents.get(node)
    return category_id


def attribute_spending(
    spent: dict[int, Decimal], budgets: dict, parents: dict[int, int | None]
) -> dict[int, Decimal]:
    """Roll per-category spending up into the envelopes that own it."""
    by_envelope: dict[int, Decimal] = {}
    for category_id, amount in spent.items():
        envelope = envelope_for(category_id, budgets, parents)
        by_envelope[envelope] = by_envelope.get(envelope, ZERO) + amount
    return by_envelope


def _actuals_by_category(ledger, month_start: date) -> tuple[Decimal, dict[int, Decimal], dict]:
    """Returns (income, spent per expense category, category meta). ``spent``
    is positive for net outflow — a reimbursement inflow reduces it."""
    income = ZERO
    spent: dict[int, Decimal] = {}
    meta: dict[int, dict] = {}
    for row in ledger.category_actuals(month_start, month_end(month_start)):
        net = Decimal(row["net"])
        if row["kind"] == "income":
            income += net
        else:
            key = row["category_id"] if row["category_id"] is not None else -1
            spent[key] = spent.get(key, ZERO) - net  # net<0 -> positive spend
            meta[key] = row
    return income, spent, meta


def _bill_set_aside(ledger, month_start: date) -> Decimal:
    """Tier 2: monthly accrual for non-monthly bills that are NOT due this
    month (a bill due this month is already tier-1 spending)."""
    total = ZERO
    accrual = ledger.bills_accrual(on=month_start)
    for bill in accrual.get("bills", []):
        months_until = bill.get("months_until_due")
        if months_until is not None and months_until > 0:
            total += Decimal(bill["monthly_load"])
    return total


def compute_range(
    db: Session, ledger, first: date, target: date, parents: dict[int, int | None] | None = None
) -> dict[date, MonthComputation]:
    """Walk month by month from ``first`` to ``target`` computing carryovers,
    goal funding, and the General Fund. Deterministic given ledger data."""
    goals = db.execute(
        select(Goal).where(Goal.active).order_by(Goal.priority, Goal.id)
    ).scalars().all()
    if parents is None:
        parents = {c["id"]: c.get("parent_id") for c in ledger.categories()}
    goal_funded: dict[int, Decimal] = {goal.id: ZERO for goal in goals}
    carry: dict[int, Decimal] = {}
    results: dict[date, MonthComputation] = {}

    # Budgets are change points that stay in force until superseded, so the
    # walk carries a running plan rather than re-reading a value per month.
    changes = budget_changes(db, through=target)
    plan: dict[int, Decimal] = {}
    for month in sorted(m for m in changes if m < first):
        plan.update(changes[month])

    month = first
    while month <= target:
        comp = MonthComputation()
        income, spent, _meta = _actuals_by_category(ledger, month)
        comp.income = income
        comp.spent_by_category = spent

        plan.update(changes.get(month, {}))
        budgets = {cid: amount for cid, amount in plan.items() if amount > 0}
        # Spending on a subcategory counts against whichever envelope owns it
        # (its own budget if it has one, otherwise its parent's).
        by_envelope = attribute_spending(spent, budgets, parents)
        comp.spent_by_envelope = by_envelope

        new_carry: dict[int, Decimal] = {}
        for category_id in set(budgets) | set(by_envelope) | set(carry):
            budgeted = budgets.get(category_id, ZERO)
            carry_in = carry.get(category_id, ZERO)
            available = budgeted + carry_in - by_envelope.get(category_id, ZERO)
            if available < 0:
                new_carry[category_id] = available  # deficits carry...
            # ...surpluses do not: they flow to the General Fund via the
            # waterfall remainder below.
        comp.carry_out = new_carry
        carry = new_carry

        total_spent = sum(spent.values(), ZERO)
        comp.bill_set_aside = _bill_set_aside(ledger, month)
        remaining = income - total_spent - comp.bill_set_aside
        for goal in goals:
            if remaining <= 0:
                break
            headroom = goal.target_amount - goal_funded[goal.id]
            if headroom <= 0:
                continue
            allocation = min(goal.monthly_contribution, headroom, remaining)
            if allocation > 0:
                comp.goal_allocations[goal.id] = allocation
                goal_funded[goal.id] += allocation
                remaining -= allocation
        comp.general_fund_delta = remaining
        results[month] = comp
        month = next_month(month)
    return results


def build_category_rows(
    comp: MonthComputation,
    budgets: dict,
    carry_in: dict,
    categories: dict,
    parents: dict[int, int | None],
) -> list[dict]:
    """Flat list of display rows in parent-then-children order.

    A row is an **envelope** (it has a budget, a carryover, or unbudgeted
    spending) or a **subtotal** — a parent with no budget of its own whose
    children have activity. Subtotal rows carry ``is_subtotal`` so the UI
    renders them read-only instead of offering a meaningless input box.
    """
    involved = set(budgets) | set(comp.spent_by_category) | set(carry_in)
    involved |= set(comp.spent_by_envelope)
    # Parents of anything involved need a row so the total is visible.
    for category_id in list(involved):
        parent_id = parents.get(category_id)
        if parent_id is not None:
            involved.add(parent_id)

    def is_expense(category_id) -> bool:
        return categories.get(category_id, {}).get("kind") != "income"

    def name_of(category_id) -> str:
        return categories.get(category_id, {}).get("name") or "(uncategorized)"

    def make_row(category_id, depth: int) -> dict:
        budgeted = budgets.get(category_id, ZERO)
        carried = carry_in.get(category_id, ZERO)
        # Does this category own an envelope, or does its spending belong to an
        # ancestor's? A child under a budgeted parent is a detail row: it shows
        # what was spent but must NOT report a deficit of its own, or every
        # subcategory would look permanently over budget.
        owns_envelope = (
            envelope_for(category_id, budgets, parents) == category_id
            or category_id in carry_in
        )
        spent = comp.spent_by_envelope.get(
            category_id, comp.spent_by_category.get(category_id, ZERO)
        )
        available = budgeted + carried - spent
        return {
            "category_id": category_id if category_id != -1 else None,
            "name": name_of(category_id),
            "depth": depth,
            "is_subtotal": False,
            "is_envelope": owns_envelope,
            "budgeted": money_str(budgeted),
            "carry_in": money_str(carried),
            "spent": money_str(spent),
            "available": money_str(available) if owns_envelope else None,
            "over": owns_envelope and available < 0,
        }

    tops = sorted(
        (cid for cid in involved if parents.get(cid) is None and is_expense(cid)),
        key=lambda cid: name_of(cid).lower(),
    )
    rows: list[dict] = []
    for top in tops:
        children = sorted(
            (cid for cid in involved if parents.get(cid) == top),
            key=lambda cid: name_of(cid).lower(),
        )
        parent_row = make_row(top, 0)
        child_rows = [make_row(child, 1) for child in children]
        if children and top not in budgets and top not in carry_in:
            # Parent is purely a heading: show it as the sum of its children
            # (plus any direct spending of its own) with no editable budget.
            own_spent = comp.spent_by_category.get(top, ZERO)
            parent_row.update(
                {
                    "is_subtotal": True,
                    "is_envelope": False,
                    "budgeted": money_str(sum(Decimal(r["budgeted"]) for r in child_rows)),
                    "carry_in": money_str(sum(Decimal(r["carry_in"]) for r in child_rows)),
                    "spent": money_str(
                        own_spent + sum(Decimal(r["spent"]) for r in child_rows)
                    ),
                    "available": money_str(
                        sum(
                            Decimal(r["available"]) for r in child_rows if r["available"] is not None
                        )
                        - own_spent
                    ),
                }
            )
            parent_row["over"] = Decimal(parent_row["available"]) < 0
        rows.append(parent_row)
        rows.extend(child_rows)
    return rows


def month_view(db: Session, ledger, month_start: date) -> dict:
    """Everything the budget page needs for one month."""
    earliest_budget = db.execute(
        select(CategoryBudget.month).order_by(CategoryBudget.month).limit(1)
    ).scalar_one_or_none()
    first = min(earliest_budget or month_start, month_start)
    category_list = ledger.categories()
    categories = {c["id"]: c for c in category_list}
    parents = {c["id"]: c.get("parent_id") for c in category_list}
    computed = compute_range(db, ledger, first, month_start, parents=parents)
    comp = computed[month_start]

    # Carry-in for the target month = carry-out of the previous month.
    months = sorted(computed)
    index = months.index(month_start)
    carry_in = computed[months[index - 1]].carry_out if index > 0 else {}

    resolved = resolve_plan(db, month_start)
    budgets = {cid: amount for cid, (amount, _since) in resolved.items()}
    changed_here = set(budget_changes(db, through=month_start).get(month_start, {}))
    rows = build_category_rows(comp, budgets, carry_in, categories, parents)
    for row in rows:
        category_id = row["category_id"]
        entry = resolved.get(category_id) if category_id is not None else None
        since = entry[1] if entry else None
        # Where this number came from, so the page can say "carried forward
        # from March" instead of implying it was typed in this month.
        row["budget_since"] = f"{since.year:04d}-{since.month:02d}" if since else None
        row["budget_inherited"] = bool(since and since != month_start)
        row["budget_changed_here"] = category_id in changed_here

    goals = db.execute(
        select(Goal).where(Goal.active).order_by(Goal.priority, Goal.id)
    ).scalars().all()
    funded_totals: dict[int, Decimal] = {goal.id: ZERO for goal in goals}
    for month_comp in computed.values():
        for goal_id, allocation in month_comp.goal_allocations.items():
            funded_totals[goal_id] = funded_totals.get(goal_id, ZERO) + allocation

    general_fund_total = sum((c.general_fund_delta for c in computed.values()), ZERO)
    month_deficit = sum((amount for amount in comp.carry_out.values()), ZERO)

    return {
        "month": f"{month_start.year:04d}-{month_start.month:02d}",
        "income": money_str(comp.income),
        "categories": rows,
        "total_budgeted": money_str(sum(budgets.values(), ZERO)),
        "total_spent": money_str(sum(comp.spent_by_category.values(), ZERO)),
        "month_deficit": money_str(month_deficit),
        "waterfall": {
            "spent": money_str(sum(comp.spent_by_category.values(), ZERO)),
            "bill_set_aside": money_str(comp.bill_set_aside),
            "goals": [
                {
                    "goal_id": goal.id,
                    "name": goal.name,
                    "allocated_this_month": money_str(comp.goal_allocations.get(goal.id, ZERO)),
                    "funded_total": money_str(funded_totals.get(goal.id, ZERO)),
                    "target_amount": money_str(goal.target_amount),
                    "progress_pct": int(
                        (funded_totals.get(goal.id, ZERO) / goal.target_amount * 100)
                        if goal.target_amount
                        else 0
                    ),
                }
                for goal in goals
            ],
            "general_fund_delta": money_str(comp.general_fund_delta),
            "general_fund_total": money_str(general_fund_total),
        },
        "global_deficit": general_fund_total < 0,
    }
