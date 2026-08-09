"""Ledger data model.

Sign convention everywhere: **positive = inflow, negative = outflow**, in the
owning account's currency.

The design choices that matter (they come straight from the requirements):

- A transaction's amount lives in its *splits*: one transaction, one total,
  optionally divided across several categories. Receipt splits re-divide the
  splits of the one transaction — they never create new transactions.
- Categories have a *default* direction (``kind``) but accept either sign.
  An inflow posted to the expense category "Rent" (a roommate's Zelle share)
  reduces net Rent spending — the MS Money "strict income/expense" wall does
  not exist here.
- Transfers are a pair of transactions sharing ``transfer_group_id`` whose
  splits have **no category**; they can never pollute spending reports.
- Asset/liability value changes are ``kind='valuation'`` transactions:
  visible in net worth, invisible in cash flow. (A car losing $38k is not a
  cash expense.)
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

ACCOUNT_TYPES = ("checking", "savings", "credit_card", "cash", "asset", "liability")
# Types whose transactions are money actually moving month to month. Asset and
# liability valuation changes are wealth, not cash flow.
CASHFLOW_ACCOUNT_TYPES = ("checking", "savings", "credit_card", "cash")

CATEGORY_KINDS = ("expense", "income")
TRANSACTION_STATUSES = ("uncleared", "cleared", "reconciled")
TRANSACTION_KINDS = ("normal", "transfer", "valuation")
ALIAS_MATCH_TYPES = ("exact", "prefix", "contains")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Currency(Base):
    __tablename__ = "currencies"

    code: Mapped[str] = mapped_column(String(3), primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    decimals: Mapped[int] = mapped_column(Integer, default=2)


class FxRate(Base):
    """Manually entered exchange rate: 1 unit of from_code = ``rate`` to_code.
    Lookups use the latest rate on/before the date and fall back to the
    inverse pair."""

    __tablename__ = "fx_rates"
    __table_args__ = (UniqueConstraint("date", "from_code", "to_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date)
    from_code: Mapped[str] = mapped_column(ForeignKey("currencies.code"))
    to_code: Mapped[str] = mapped_column(ForeignKey("currencies.code"))
    rate: Mapped[Decimal] = mapped_column(Numeric(18, 8))


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    type: Mapped[str] = mapped_column(String(20))
    currency_code: Mapped[str] = mapped_column(ForeignKey("currencies.code"), default="USD")
    opening_balance: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("name", "parent_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(10), default="expense")
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    parent: Mapped["Category | None"] = relationship(remote_side="Category.id")


class Payee(Base):
    __tablename__ = "payees"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    default_category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class PayeeAlias(Base):
    """A normalized CSV description pattern that identifies a payee. Created
    automatically when the user resolves an unknown description during import,
    so the next import maps it without asking."""

    __tablename__ = "payee_aliases"
    __table_args__ = (UniqueConstraint("pattern", "match_type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    payee_id: Mapped[int] = mapped_column(ForeignKey("payees.id"))
    pattern: Mapped[str] = mapped_column(String(300))
    match_type: Mapped[str] = mapped_column(String(10), default="exact")

    payee: Mapped[Payee] = relationship()


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    payee_id: Mapped[int | None] = mapped_column(ForeignKey("payees.id"), nullable=True)
    memo: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(12), default="uncleared")
    kind: Mapped[str] = mapped_column(String(10), default="normal", index=True)
    transfer_group_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    import_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    account: Mapped[Account] = relationship()
    payee: Mapped[Payee | None] = relationship()
    splits: Mapped[list["Split"]] = relationship(
        back_populates="transaction", cascade="all, delete-orphan", order_by="Split.id"
    )

    @property
    def total(self) -> Decimal:
        return sum((split.amount for split in self.splits), Decimal("0"))


class Split(Base):
    """One category's share of a transaction. category_id is NULL for the
    splits of transfers and valuations (they have no category by design) and
    for not-yet-categorized imports."""

    __tablename__ = "transaction_splits"

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), index=True
    )
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    memo: Mapped[str] = mapped_column(Text, default="")

    transaction: Mapped[Transaction] = relationship(back_populates="splits")
    category: Mapped[Category | None] = relationship()


DEFAULT_CURRENCIES = [
    ("USD", "US Dollar", 2),
    ("CAD", "Canadian Dollar", 2),
    ("TWD", "New Taiwan Dollar", 2),
]

# Starter categories offered on first run (POST /api/seed-defaults).
DEFAULT_CATEGORIES = {
    "income": ["Salary", "Interest", "Other Income"],
    "expense": [
        "Rent",
        "Groceries",
        "Dining Out",
        "Utilities",
        "Transportation",
        "Insurance",
        "Medical",
        "Entertainment",
        "Shopping",
        "Travel",
        "Subscriptions",
        "Fees",
        "Miscellaneous",
    ],
}
