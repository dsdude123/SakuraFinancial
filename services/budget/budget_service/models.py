"""Budget data model.

The budget service stores only intent: how much the user planned per category
per month, and the savings goals. Everything else — spending, income, bill
accruals — is derived live from the ledger service, so the budget can never
drift out of sync with the register.

Category IDs reference the ledger's categories by ID over the API (services
own separate databases; there are no cross-service foreign keys).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import Boolean, Date, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class CategoryBudget(Base):
    """A **change** to one category's planned amount, effective from ``month``
    onward (month stored as its first day).

    Rows are not per-month values. A budget set in March is the budget for
    April, May and every month after, until another row for that category
    supersedes it — nobody should have to re-enter the same figures twelve
    times a year. ``logic.resolve_plan`` turns these change points into the
    plan in force for any given month.

    ``amount == 0`` is a stop, not a value: it means "this category is no
    longer budgeted from this month on", which is how a category leaves the
    budget without rewriting history. Deleting the row instead reverts the
    month to whatever the previous change said.
    """

    __tablename__ = "category_budgets"
    __table_args__ = (UniqueConstraint("month", "category_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    month: Mapped[date] = mapped_column(Date, index=True)
    category_id: Mapped[int] = mapped_column(Integer, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4))


class Goal(Base):
    """A savings goal funded by the auto-assignment waterfall (tier 3).
    ``monthly_contribution`` is the planned pace; funding stops at
    ``target_amount``."""

    __tablename__ = "goals"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150))
    target_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    monthly_contribution: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(Text, default="")
