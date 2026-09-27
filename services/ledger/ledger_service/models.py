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
- Money can also move to an account this service does not own — a brokerage
  or RSU account lives in the stocks service. That transfer is a *single*
  leg here, carrying ``external_account`` ("stock:1") instead of a second
  ledger transaction; the far side is a matching cash row in the other
  service sharing the same ``transfer_group_id``. It is still a categoryless
  ``kind='transfer'``, so cash-flow reports ignore it, and net worth stays
  right because the other service reports the money it received.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    JSON,
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
ALIAS_MATCH_TYPES = ("exact", "regex", "prefix", "contains")


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
    r"""A CSV description pattern that identifies a payee. Created automatically
    when the user resolves an unknown description during import, so the next
    import maps it without asking.

    ``exact``/``prefix``/``contains`` patterns are stored normalized (upper
    case, whitespace collapsed) and compared against the normalized
    description. ``regex`` patterns are stored **verbatim** and matched
    case-insensitively: normalizing one would corrupt it, since upper-casing
    turns ``\d`` into ``\D`` and inverts its meaning. Regex is what handles
    the statements where one merchant arrives with a different reference
    number every time.
    """

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
    # Set only on a transfer whose other side is not a ledger account: the ref
    # of an account owned by another service, "<service>:<id>" (today only
    # "stock:<id>"). Such a transfer has one leg here; ``transfer_group_id``
    # ties it to the cash row the other service booked.
    external_account: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
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


BILL_FREQUENCIES = ("weekly", "biweekly", "monthly", "quarterly", "semiannual", "annual")
BILL_OCCURRENCE_STATUSES = ("upcoming", "paid", "skipped", "amount_review")


class Bill(Base):
    """A recurring obligation. ``amount`` is the expected outflow (positive);
    ``is_variable`` bills accept any matched amount, fixed bills flag
    ``amount_review`` when the matched amount differs."""

    __tablename__ = "bills"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150))
    payee_id: Mapped[int] = mapped_column(ForeignKey("payees.id"))
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    frequency: Mapped[str] = mapped_column(String(12), default="monthly")
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    is_variable: Mapped[bool] = mapped_column(Boolean, default=False)
    next_due: Mapped[date] = mapped_column(Date)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(Text, default="")

    payee: Mapped[Payee] = relationship()
    category: Mapped[Category | None] = relationship()
    occurrences: Mapped[list["BillOccurrence"]] = relationship(
        back_populates="bill", cascade="all, delete-orphan", order_by="BillOccurrence.due_date"
    )


class BillOccurrence(Base):
    __tablename__ = "bill_occurrences"
    __table_args__ = (UniqueConstraint("bill_id", "due_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bill_id: Mapped[int] = mapped_column(ForeignKey("bills.id", ondelete="CASCADE"), index=True)
    due_date: Mapped[date] = mapped_column(Date, index=True)
    expected_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    actual_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    status: Mapped[str] = mapped_column(String(15), default="upcoming", index=True)
    matched_transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL"), nullable=True
    )

    bill: Mapped[Bill] = relationship(back_populates="occurrences")


class TransferRule(Base):
    """Imported descriptions matching the pattern become transfers to/from
    ``account_id`` instead of categorized transactions (PAYPAL, VENMO,
    credit-card payments...). Patterns match the normalized description."""

    __tablename__ = "transfer_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    pattern: Mapped[str] = mapped_column(String(300))
    match_type: Mapped[str] = mapped_column(String(10), default="prefix")
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # How far apart the two sides of one transfer may be dated. Money leaving
    # on a Friday can land in the other account the following Tuesday, and both
    # statements report their own date, so matching needs slack.
    match_days: Mapped[int] = mapped_column(Integer, default=5)

    account: Mapped[Account] = relationship()


class ImportProfile(Base):
    """How to read one institution's CSV files — see
    sakura_common.csvengine for the config keys."""

    __tablename__ = "import_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    account: Mapped[Account | None] = relationship()


IMPORT_BATCH_STATUSES = ("review", "committed", "aborted")
IMPORT_ROW_STATUSES = ("ready", "needs_payee", "duplicate", "transfer", "counterpart")


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    profile_id: Mapped[int] = mapped_column(ForeignKey("import_profiles.id"))
    filename: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(12), default="review")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    account: Mapped[Account] = relationship()
    profile: Mapped[ImportProfile] = relationship()
    rows: Mapped[list["ImportRow"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="ImportRow.line_no"
    )


class ImportRow(Base):
    """One parsed CSV row awaiting review. Rows only become transactions when
    the batch is committed; until then nothing touches the register."""

    __tablename__ = "import_rows"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id", ondelete="CASCADE"), index=True)
    line_no: Mapped[int] = mapped_column(Integer)
    date: Mapped[date] = mapped_column(Date)
    description: Mapped[str] = mapped_column(Text)
    memo: Mapped[str] = mapped_column(Text, default="")
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    row_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(12), default="needs_payee")
    include: Mapped[bool] = mapped_column(Boolean, default=True)
    payee_id: Mapped[int | None] = mapped_column(ForeignKey("payees.id"), nullable=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    transfer_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    learn_alias: Mapped[bool] = mapped_column(Boolean, default=False)
    transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id"), nullable=True)

    batch: Mapped[ImportBatch] = relationship(back_populates="rows")
    payee: Mapped[Payee | None] = relationship()


DEFAULT_CURRENCIES = [
    ("USD", "US Dollar", 2),
    ("CAD", "Canadian Dollar", 2),
    ("TWD", "New Taiwan Dollar", 2),
]

# Starter categories offered on first run (POST /api/seed-defaults).
# A value of None is a plain category; a list creates subcategories under it,
# so spending rolls up ("Food" total, broken into Groceries vs Dining Out).
DEFAULT_CATEGORIES: dict[str, dict[str, list[str] | None]] = {
    "income": {"Salary": None, "Interest": None, "Other Income": None},
    "expense": {
        "Rent": None,
        "Food": ["Groceries", "Dining Out", "Coffee"],
        "Utilities": ["Electric", "Gas", "Water", "Internet"],
        "Transportation": ["Fuel", "Transit", "Parking", "Maintenance"],
        "Insurance": None,
        "Medical": None,
        "Entertainment": None,
        "Shopping": None,
        "Travel": None,
        "Subscriptions": None,
        "Fees": None,
        "Miscellaneous": None,
    },
}
