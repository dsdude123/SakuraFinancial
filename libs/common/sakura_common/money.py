"""Decimal-safe money parsing and formatting.

All amounts in SakuraFinancial are `decimal.Decimal` end-to-end and are stored
as NUMERIC in the database. Floats are never used for money. Amounts are signed:
positive = inflow, negative = outflow.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

# Display decimals per ISO 4217 code for the currencies the platform ships
# with. Anything unknown falls back to 2.
CURRENCY_DECIMALS: dict[str, int] = {
    "USD": 2,
    "CAD": 2,
    "TWD": 2,  # New Taiwan Dollar (NTD)
    "JPY": 0,
}

_CLEAN_RE = re.compile(r"[,\s$€£¥]|(?:USD|CAD|TWD|NTD)$", re.IGNORECASE)


class AmountParseError(ValueError):
    """Raised when a string cannot be interpreted as a monetary amount."""


def parse_amount(raw: str) -> Decimal:
    """Parse a human/bank-CSV amount string into a Decimal.

    Accepts thousands separators, currency symbols/codes, a leading sign, and
    accountants' parentheses for negatives: "(12.34)" -> Decimal("-12.34").

    Raises AmountParseError with the offending text so CSV import can show a
    per-row error instead of silently importing garbage.
    """
    text = raw.strip()
    if not text:
        raise AmountParseError("amount is empty")
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]
    text = _CLEAN_RE.sub("", text).strip()
    if text.startswith("+"):
        text = text[1:]
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise AmountParseError(f"not a number: {raw!r}") from exc
    return -value if negative else value


def quantize(amount: Decimal, currency: str = "USD") -> Decimal:
    """Round an amount to its currency's display precision (half-up)."""
    decimals = CURRENCY_DECIMALS.get(currency.upper(), 2)
    return amount.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)


def format_amount(amount: Decimal, currency: str = "USD", with_code: bool = False) -> str:
    """Format for display: thousands separators, currency precision.

    format_amount(Decimal("-1234.5"), "USD") -> "-1,234.50"
    """
    decimals = CURRENCY_DECIMALS.get(currency.upper(), 2)
    quantized = quantize(amount, currency)
    text = f"{quantized:,.{decimals}f}"
    return f"{text} {currency.upper()}" if with_code else text
