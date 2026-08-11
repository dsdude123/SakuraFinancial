"""Stocks data model.

Three kinds of investment account, straight from the requirements:

- ``brokerage``: normal account, buy/sell freely.
- ``rsu``: holds restricted stock units — grants vest on a schedule into
  sellable lots, and the account can carry a trading window.
- ``managed``: e.g. an E*TRADE smart portfolio. The user only moves cash in
  and out; holdings appear via CSV import, and the UI hides trade entry and
  analysis for these — performance tracking only.

Cash in an account is derived: opening_cash + the signed cash effects of its
transactions. Prices are a per-security daily history (Yahoo or manual), so
charts and valuations work from the day a security enters the system.
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

ACCOUNT_TYPES = ("brokerage", "rsu", "managed")
TXN_TYPES = ("buy", "sell", "dividend", "vest", "deposit", "withdraw", "fee")
PRICE_SOURCES = ("yahoo", "manual")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class InvestmentAccount(Base):
    __tablename__ = "investment_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    type: Mapped[str] = mapped_column(String(12))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    opening_cash: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0"))
    trading_window_open: Mapped[date | None] = mapped_column(Date, nullable=True)
    trading_window_close: Mapped[date | None] = mapped_column(Date, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Security(Base):
    __tablename__ = "securities"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(150), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Lot(Base):
    """A parcel of shares with its own cost basis and acquisition date —
    the unit of FIFO selling and of unrealized-gain math."""

    __tablename__ = "lots"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("investment_accounts.id"), index=True)
    security_id: Mapped[int] = mapped_column(ForeignKey("securities.id"), index=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    cost_basis: Mapped[Decimal] = mapped_column(Numeric(18, 4))  # total for the lot
    acquired_date: Mapped[date] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(10), default="buy")  # buy|vest|manual

    security: Mapped[Security] = relationship()
    account: Mapped[InvestmentAccount] = relationship()


class RsuGrant(Base):
    __tablename__ = "rsu_grants"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("investment_accounts.id"), index=True)
    security_id: Mapped[int] = mapped_column(ForeignKey("securities.id"))
    grant_date: Mapped[date] = mapped_column(Date)
    note: Mapped[str] = mapped_column(Text, default="")

    security: Mapped[Security] = relationship()
    vesting_events: Mapped[list["VestingEvent"]] = relationship(
        back_populates="grant", cascade="all, delete-orphan", order_by="VestingEvent.vest_date"
    )


class VestingEvent(Base):
    __tablename__ = "vesting_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    grant_id: Mapped[int] = mapped_column(ForeignKey("rsu_grants.id", ondelete="CASCADE"), index=True)
    vest_date: Mapped[date] = mapped_column(Date)
    shares: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    released: Mapped[bool] = mapped_column(Boolean, default=False)
    lot_id: Mapped[int | None] = mapped_column(ForeignKey("lots.id"), nullable=True)

    grant: Mapped[RsuGrant] = relationship(back_populates="vesting_events")


class StockTransaction(Base):
    """``amount`` is the signed CASH effect on the account (buy negative,
    sell/dividend/deposit positive, withdraw/fee negative)."""

    __tablename__ = "stock_transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("investment_accounts.id"), index=True)
    security_id: Mapped[int | None] = mapped_column(ForeignKey("securities.id"), nullable=True)
    type: Mapped[str] = mapped_column(String(10))
    date: Mapped[date] = mapped_column(Date, index=True)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0"))
    fees: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0"))
    realized_gain: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    import_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    security: Mapped[Security | None] = relationship()


class Price(Base):
    __tablename__ = "prices"
    __table_args__ = (UniqueConstraint("security_id", "date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    security_id: Mapped[int] = mapped_column(ForeignKey("securities.id"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    close: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    source: Mapped[str] = mapped_column(String(10), default="yahoo")

    security: Mapped[Security] = relationship()


class StockImportProfile(Base):
    """CSV shape for one broker, including the action_map that translates the
    broker's transaction-type strings to internal actions — see
    sakura_common.csvengine.parse_stock_csv."""

    __tablename__ = "stock_import_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("investment_accounts.id"), nullable=True
    )
    config: Mapped[dict] = mapped_column(JSON, default=dict)


class StockImportBatch(Base):
    __tablename__ = "stock_import_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("investment_accounts.id"))
    profile_id: Mapped[int] = mapped_column(ForeignKey("stock_import_profiles.id"))
    filename: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(12), default="review")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    account: Mapped[InvestmentAccount] = relationship()
    rows: Mapped[list["StockImportRow"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="StockImportRow.line_no"
    )


class StockImportRow(Base):
    __tablename__ = "stock_import_rows"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("stock_import_batches.id", ondelete="CASCADE"), index=True
    )
    line_no: Mapped[int] = mapped_column(Integer)
    date: Mapped[date] = mapped_column(Date)
    action: Mapped[str] = mapped_column(String(10))
    symbol: Mapped[str] = mapped_column(String(20), default="")
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    fee: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    row_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(12), default="ready")  # ready|duplicate
    include: Mapped[bool] = mapped_column(Boolean, default=True)
    transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("stock_transactions.id"), nullable=True
    )

    batch: Mapped[StockImportBatch] = relationship(back_populates="rows")


class AnalysisResult(Base):
    __tablename__ = "analysis_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("investment_accounts.id"), nullable=True
    )
    symbol: Mapped[str] = mapped_column(String(20), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    fundamentals: Mapped[dict] = mapped_column(JSON, default=dict)
    ai_text: Mapped[str | None] = mapped_column(Text, nullable=True)
