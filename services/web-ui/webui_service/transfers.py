"""Cash moving between a bank account and an investment account.

The two ends live in different services: checking is a ledger account, a
brokerage is a stocks account. Neither service can write the other's row, so
the web UI is what makes one movement out of two calls — the same job it
already does for imports, where one review screen drives two importers.

The shape of the movement:

- the **ledger** books a single categoryless ``kind='transfer'`` leg against
  the bank account, carrying ``external_account="stock:1"``,
- the **stocks** service books the matching ``deposit``/``withdraw`` carrying
  ``external_account="bank:3"``,
- both rows share a ``transfer_group_id``, which is what lets either side
  find (and delete) the other.

Why not a category: funding a brokerage is not spending, and an expense
category for it would land in every spending report and every budget envelope
forever. Why not two ledger rows: the brokerage's cash is the stocks
service's to report — net worth already adds the ledger's balances to the
stocks valuation, so writing the arrival here as well would count it twice.

Ordering and failure: the ledger leg goes first because only it has an id we
can undo cheaply. If the stocks leg then fails, the ledger leg is deleted and
nothing is left behind. If that compensating delete *also* fails (the ledger
went away mid-request), the caller is told which transaction to remove by
hand — silently leaving half a transfer is the one outcome worth shouting
about.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from .clients import ServiceError
from .refs import join_ref


class TransferError(Exception):
    """A cross-service transfer could not be completed. ``detail`` is written
    for the user, not the log."""

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


def parse_amount(value: str, what: str = "amount") -> Decimal:
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, ArithmeticError):
        raise TransferError(f"{what} '{value}' is not a number") from None
    if parsed <= 0:
        raise TransferError(f"{what} must be positive")
    return parsed


async def transfer_bank_stock(
    clients,
    *,
    bank_account_id: int,
    stock_account_id: int,
    date: str,
    amount: str,
    to_amount: str = "",
    to_stock: bool = True,
    memo: str = "",
) -> dict:
    """Move cash between a ledger account and an investment account.

    ``to_stock`` is the direction: True funds the investment account, False
    takes money out of it. ``amount`` is in the bank account's currency;
    ``to_amount`` is what the other side received and is required when the two
    accounts are in different currencies (the same rule the ledger applies to
    its own cross-currency transfers).
    """
    sent = parse_amount(amount)
    received = parse_amount(to_amount, "the other side's amount") if str(to_amount).strip() else None
    if not str(date).strip():
        raise TransferError("a transfer needs a date")

    try:
        bank = await clients.ledger.get(f"/api/accounts/{bank_account_id}")
        stock = await clients.stocks.get(f"/api/accounts/{stock_account_id}")
    except ServiceError as exc:
        raise TransferError(str(exc.detail)) from exc

    if bank["currency_code"] != stock["currency"] and received is None:
        raise TransferError(
            f"accounts use different currencies ({bank['currency_code']} -> "
            f"{stock['currency']}); fill in the other side's amount"
        )
    stock_amount = received if received is not None else sent

    try:
        leg = await clients.ledger.post(
            "/api/transfers/external",
            json={
                "account_id": bank_account_id,
                "date": date,
                "amount": str(sent),
                "direction": "out" if to_stock else "in",
                "external_account": join_ref("stock", stock_account_id),
                "external_name": stock["name"],
                "memo": memo,
            },
        )
    except ServiceError as exc:
        raise TransferError(str(exc.detail)) from exc

    try:
        far_leg = await clients.stocks.post(
            "/api/transfers/external",
            json={
                "account_id": stock_account_id,
                "date": date,
                "amount": str(stock_amount),
                "direction": "in" if to_stock else "out",
                "external_account": join_ref("bank", bank_account_id),
                "external_name": bank["name"],
                "transfer_group_id": leg["transfer_group_id"],
                "note": memo,
            },
        )
    except ServiceError as exc:
        try:
            await clients.ledger.delete(f"/api/transactions/{leg['id']}")
        except ServiceError:
            raise TransferError(
                f"the brokerage side failed ({exc.detail}) and the {bank['name']} side "
                f"could not be undone — delete transaction {leg['id']} in "
                f"{bank['name']} by hand so the transfer isn't counted"
            ) from exc
        raise TransferError(f"{exc.detail} (nothing was saved)") from exc

    return {
        "transfer_group_id": leg["transfer_group_id"],
        "bank_leg": leg,
        "stock_leg": far_leg,
        "bank_name": bank["name"],
        "stock_name": stock["name"],
    }


async def delete_external_counterpart(clients, deleted: dict) -> None:
    """Remove the far side of a transfer whose ledger leg has just been deleted.

    ``deleted`` is the ledger's delete response. A leg with no
    ``external_account`` is an ordinary transaction and there is nothing to do.
    """
    ref = deleted.get("external_account") or ""
    group = deleted.get("transfer_group_id")
    if not ref or not group or not ref.startswith("stock:"):
        return
    await clients.stocks.delete(f"/api/transfers/external/{group}")
