"""Jinja2 environment + the filters templates lean on."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

TEMPLATE_DIR = Path(__file__).parent / "templates"


def money(value, currency: str = "") -> str:
    """'1234.5' -> '1,234.50'. Falls back to the raw value on bad input so a
    template never 500s over formatting."""
    if value is None or value == "":
        return ""
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return str(value)
    text = f"{amount:,.2f}"
    return f"{text} {currency}" if currency else text


def is_negative(value) -> bool:
    try:
        return Decimal(str(value)) < 0
    except (InvalidOperation, TypeError):
        return False


# Raw enum values are for the database, not the screen. Anything not listed
# falls back to "needs_payee" -> "Needs payee", which covers the tidy cases;
# the table is for the ones where a better phrase exists.
LABELS = {
    # account types
    "credit_card": "Credit card",
    "checking": "Checking",
    "savings": "Savings",
    "cash": "Cash",
    "asset": "Asset",
    "liability": "Liability",
    "brokerage": "Brokerage",
    "rsu": "RSU",
    "managed": "Managed",
    # import row statuses
    "ready": "Ready",
    "needs_payee": "Needs a payee",
    "duplicate": "Already imported",
    "transfer": "Transfer",
    "counterpart": "Other side of a transfer",
    "no_lots": "No shares held",
    # import batch statuses
    "review": "Awaiting review",
    "committed": "Imported",
    "aborted": "Discarded",
    # transaction status and kind
    "uncleared": "Uncleared",
    "cleared": "Cleared",
    "reconciled": "Reconciled",
    "normal": "Normal",
    "valuation": "Value change",
    # stock actions
    "buy": "Buy",
    "sell": "Sell",
    "dividend": "Dividend",
    "vest": "Vest",
    "deposit": "Deposit",
    "withdraw": "Withdrawal",
    "fee": "Fee",
    "ignore": "Ignored",
    # alias / rule match types
    "exact": "Exact match",
    "regex": "Regular expression",
    "prefix": "Starts with",
    "contains": "Contains",
    # category kinds
    "expense": "Expense",
    "income": "Income",
}


def humanize(value) -> str:
    """A raw stored value as something a person would read."""
    if value is None or value == "":
        return ""
    text = str(value)
    return LABELS.get(text, text.replace("_", " ").capitalize())


def friendly(message) -> str:
    """Tidy a backend error for display: sentence case, closing full stop.

    Service messages are written for an API reader and arrive lower-cased and
    unpunctuated. This doesn't fix a badly worded message, but it stops every
    one of them looking like a log line."""
    if message is None:
        return ""
    text = str(message).strip()
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    if text[-1] not in ".!?)":
        text += "."
    return text


def make_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    templates.env.filters["money"] = money
    templates.env.filters["humanize"] = humanize
    templates.env.filters["friendly"] = friendly
    templates.env.tests["negative"] = is_negative
    return templates


def render(request: Request, name: str, context: dict | None = None):
    templates: Jinja2Templates = request.app.state.templates
    context = dict(context or {})
    context.setdefault("msg", request.query_params.get("msg", ""))
    context.setdefault("err", request.query_params.get("err", ""))
    return templates.TemplateResponse(request, name, context)
