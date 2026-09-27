"""Profile-driven CSV parsing with all-or-nothing validation.

Different banks ship different CSV shapes, so every import goes through a
stored *profile* describing the file: delimiter, header handling, which
column is which, the date format, and the sign convention. Parsing validates
the **entire file first and raises with every row error at once** — the
import UI shows the full error report and nothing is written until a file
parses completely. That rule comes straight from the requirements.

Bank profile keys:
    delimiter        default ","
    has_header       default true; column refs are header names (else indexes)
    skip_top_rows    content lines above the header/data, default 0.
                     **Blank lines never count** — brokers pad their preamble
                     with them and nobody should have to count invisible rows.
    date_column      required
    date_format      strptime format, default "%m/%d/%Y"
    description_column  required
    memo_column      optional
    amount_mode      "single" (default) or "debit_credit"
    amount_column    required for single mode
    debit_column / credit_column   required for debit_credit mode
    negate_amount    default false — set when the bank writes charges positive
    null_tokens      placeholder strings that mean "no value" in optional
                     columns, default ``DEFAULT_NULL_TOKENS`` ("--", "n/a", ...)

Stock profile keys (used by the stocks service):
    delimiter / has_header / skip_top_rows / date_column / date_format /
    null_tokens as above
    action_column    required — the broker's transaction-type string
    action_map       required — {"Bought": "buy", "YOU SOLD": "sell", ...}
                     an unmapped action string is a row error (all-or-nothing)
    symbol_column    optional (cash-only actions may have none)
    quantity_column / price_column / fee_column / amount_column  optional
    description_column  optional

Share quantities are stored as magnitudes: brokers write a sale as a negative
quantity ("-1.500") and the direction already lives in the action, so buy/sell/
vest quantities are normalized to positive here.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from .money import AmountParseError, parse_amount

STOCK_ACTIONS = ("buy", "sell", "dividend", "vest", "deposit", "withdraw", "fee", "ignore")

# Brokers fill columns that don't apply to a row with a placeholder rather than
# leaving them empty — a cash deposit gets "--" in the Symbol column. Treated as
# blank in *optional* columns only; a required column holding one is still an
# error the user needs to see.
DEFAULT_NULL_TOKENS = ("--", "---", "n/a", "na", "none", "null")


@dataclass
class RowError:
    line_no: int
    message: str
    raw: str

    def as_dict(self) -> dict:
        return {"line_no": self.line_no, "message": self.message, "raw": self.raw}


class CsvValidationError(Exception):
    """The file failed validation. Carries every row error so the user can fix
    the profile (or the file) in one pass."""

    def __init__(self, errors: list[RowError]):
        self.errors = errors
        super().__init__(f"{len(errors)} row error(s)")

    def as_dicts(self) -> list[dict]:
        return [error.as_dict() for error in self.errors]


@dataclass
class ParsedBankRow:
    line_no: int
    date: date
    description: str
    amount: Decimal
    memo: str = ""


@dataclass
class ParsedStockRow:
    line_no: int
    date: date
    action: str
    symbol: str = ""
    quantity: Decimal | None = None
    price: Decimal | None = None
    fee: Decimal | None = None
    amount: Decimal | None = None
    description: str = ""


@dataclass
class _Grid:
    header: list[str] | None
    rows: list[tuple[int, list[str]]]  # (1-based line number, cells)
    errors: list[RowError] = field(default_factory=list)


def _read_grid(text: str, profile: dict) -> _Grid:
    """Drop blank lines, skip the profile's preamble, then split off the header.

    Blank lines are removed *before* ``skip_top_rows`` is applied, so the number
    the user types is the number of junk lines they can actually see in the
    file. Line numbers stay true to the original file so error reports point at
    the right line."""
    delimiter = profile.get("delimiter", ",")
    skip_top = int(profile.get("skip_top_rows", 0))
    has_header = bool(profile.get("has_header", True))
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    all_rows = [
        (index + 1, row)
        for index, row in enumerate(reader)
        if any(cell.strip() for cell in row)
    ]
    all_rows = all_rows[skip_top:]
    header = None
    if has_header:
        if not all_rows:
            raise CsvValidationError([RowError(1, "file has no header row", "")])
        header = [cell.strip().lower() for cell in all_rows[0][1]]
        all_rows = all_rows[1:]
    if not all_rows:
        raise CsvValidationError([RowError(1, "file contains no data rows", "")])
    return _Grid(header=header, rows=all_rows)


class _RowReader:
    """Resolves a profile's column reference (header name or index) to a cell
    value, recording an error instead of raising."""

    def __init__(self, grid: _Grid, profile: dict):
        self.grid = grid
        self.profile = profile
        tokens = profile.get("null_tokens")
        if tokens is None:
            tokens = DEFAULT_NULL_TOKENS
        self.null_tokens = {str(token).strip().lower() for token in tokens}

    def resolve_index(self, column_ref, line_no: int, row: list[str], errors: list[RowError]):
        if column_ref is None or column_ref == "":
            return None
        if self.grid.header is not None and not str(column_ref).isdigit():
            name = str(column_ref).strip().lower()
            if name not in self.grid.header:
                errors.append(
                    RowError(
                        line_no,
                        f"profile references column {column_ref!r} but the file has "
                        f"columns {self.grid.header}",
                        ",".join(row),
                    )
                )
                return None
            return self.grid.header.index(name)
        try:
            index = int(column_ref)
        except (TypeError, ValueError):
            errors.append(
                RowError(line_no, f"bad column reference {column_ref!r} in profile", ",".join(row))
            )
            return None
        return index

    def cell(self, row: list[str], index: int | None) -> str:
        if index is None or index >= len(row):
            return ""
        return row[index].strip()

    def optional_cell(self, row: list[str], index: int | None) -> str:
        """A cell from a column that may not apply to this row: the broker's
        placeholder ("--", "N/A", ...) reads as empty rather than as data."""
        value = self.cell(row, index)
        return "" if value.lower() in self.null_tokens else value


def _parse_date_cell(value: str, date_format: str, line_no: int, raw: str, errors: list[RowError]):
    if not value:
        errors.append(RowError(line_no, "date is empty", raw))
        return None
    try:
        return datetime.strptime(value, date_format).date()
    except ValueError:
        errors.append(
            RowError(line_no, f"date {value!r} does not match format {date_format!r}", raw)
        )
        return None


def _parse_amount_cell(value: str, line_no: int, raw: str, errors: list[RowError], field_name="amount"):
    try:
        return parse_amount(value)
    except AmountParseError as exc:
        errors.append(RowError(line_no, f"{field_name}: {exc}", raw))
        return None


def parse_bank_csv(text: str, profile: dict) -> list[ParsedBankRow]:
    """Parse a bank/credit-card CSV. Raises CsvValidationError carrying every
    row error if anything at all is wrong; returns fully valid rows otherwise."""
    grid = _read_grid(text, profile)
    reader = _RowReader(grid, profile)
    errors: list[RowError] = []
    parsed: list[ParsedBankRow] = []
    date_format = profile.get("date_format", "%m/%d/%Y")
    amount_mode = profile.get("amount_mode", "single")
    negate = bool(profile.get("negate_amount", False))

    for line_no, row in grid.rows:
        raw = ",".join(row)
        row_errors: list[RowError] = []
        date_index = reader.resolve_index(profile.get("date_column"), line_no, row, row_errors)
        desc_index = reader.resolve_index(
            profile.get("description_column"), line_no, row, row_errors
        )
        memo_index = reader.resolve_index(profile.get("memo_column"), line_no, row, row_errors)
        if profile.get("date_column") in (None, ""):
            row_errors.append(RowError(line_no, "profile has no date_column", raw))
        if profile.get("description_column") in (None, ""):
            row_errors.append(RowError(line_no, "profile has no description_column", raw))

        when = _parse_date_cell(reader.cell(row, date_index), date_format, line_no, raw, row_errors) if date_index is not None else None
        description = reader.cell(row, desc_index) if desc_index is not None else ""
        if desc_index is not None and not description:
            row_errors.append(RowError(line_no, "description is empty", raw))

        amount: Decimal | None = None
        if amount_mode == "single":
            amount_index = reader.resolve_index(
                profile.get("amount_column"), line_no, row, row_errors
            )
            if profile.get("amount_column") in (None, ""):
                row_errors.append(RowError(line_no, "profile has no amount_column", raw))
            elif amount_index is not None:
                amount = _parse_amount_cell(reader.cell(row, amount_index), line_no, raw, row_errors)
        elif amount_mode == "debit_credit":
            debit_index = reader.resolve_index(profile.get("debit_column"), line_no, row, row_errors)
            credit_index = reader.resolve_index(
                profile.get("credit_column"), line_no, row, row_errors
            )
            debit_text = reader.cell(row, debit_index) if debit_index is not None else ""
            credit_text = reader.cell(row, credit_index) if credit_index is not None else ""
            if debit_text and credit_text:
                row_errors.append(
                    RowError(line_no, "row has both a debit and a credit value", raw)
                )
            elif debit_text:
                debit = _parse_amount_cell(debit_text, line_no, raw, row_errors, "debit")
                amount = -abs(debit) if debit is not None else None
            elif credit_text:
                credit = _parse_amount_cell(credit_text, line_no, raw, row_errors, "credit")
                amount = abs(credit) if credit is not None else None
            else:
                row_errors.append(RowError(line_no, "row has neither debit nor credit", raw))
        else:
            row_errors.append(
                RowError(line_no, f"profile amount_mode {amount_mode!r} is not valid", raw)
            )

        errors.extend(row_errors)
        if not row_errors and when is not None and amount is not None:
            if negate:
                amount = -amount
            parsed.append(
                ParsedBankRow(
                    line_no=line_no,
                    date=when,
                    description=description,
                    amount=amount,
                    memo=reader.optional_cell(row, memo_index) if memo_index is not None else "",
                )
            )
    if errors:
        raise CsvValidationError(errors)
    return parsed


def parse_stock_csv(text: str, profile: dict) -> list[ParsedStockRow]:
    """Parse a brokerage CSV using the profile's action_map. An action string
    with no mapping is a row error: the user is told to extend the map, and
    nothing imports until every row resolves. Actions mapped to "ignore" are
    dropped (headers like 'Journal' lines some brokers add)."""
    grid = _read_grid(text, profile)
    reader = _RowReader(grid, profile)
    errors: list[RowError] = []
    parsed: list[ParsedStockRow] = []
    date_format = profile.get("date_format", "%m/%d/%Y")
    action_map = {
        str(key).strip().lower(): value for key, value in (profile.get("action_map") or {}).items()
    }

    for line_no, row in grid.rows:
        raw = ",".join(row)
        row_errors: list[RowError] = []
        if profile.get("date_column") in (None, ""):
            row_errors.append(RowError(line_no, "profile has no date_column", raw))
        if profile.get("action_column") in (None, ""):
            row_errors.append(RowError(line_no, "profile has no action_column", raw))
        if not action_map:
            row_errors.append(RowError(line_no, "profile has no action_map", raw))
        date_index = reader.resolve_index(profile.get("date_column"), line_no, row, row_errors)
        action_index = reader.resolve_index(profile.get("action_column"), line_no, row, row_errors)

        when = _parse_date_cell(reader.cell(row, date_index), date_format, line_no, raw, row_errors) if date_index is not None else None

        action = None
        action_text = reader.cell(row, action_index) if action_index is not None else ""
        if action_index is not None:
            mapped = action_map.get(action_text.strip().lower())
            if mapped is None:
                row_errors.append(
                    RowError(
                        line_no,
                        f"action {action_text!r} has no mapping in the profile's action map — "
                        f"add it (one of {STOCK_ACTIONS})",
                        raw,
                    )
                )
            elif mapped not in STOCK_ACTIONS:
                row_errors.append(
                    RowError(
                        line_no,
                        f"action map sends {action_text!r} to unknown action {mapped!r} "
                        f"(valid: {STOCK_ACTIONS})",
                        raw,
                    )
                )
            else:
                action = mapped

        def optional_decimal(key: str, label: str):
            index = reader.resolve_index(profile.get(key), line_no, row, row_errors)
            value = reader.optional_cell(row, index) if index is not None else ""
            if not value:
                return None
            return _parse_amount_cell(value, line_no, raw, row_errors, label)

        symbol_index = reader.resolve_index(profile.get("symbol_column"), line_no, row, row_errors)
        desc_index = reader.resolve_index(
            profile.get("description_column"), line_no, row, row_errors
        )
        quantity = optional_decimal("quantity_column", "quantity")
        price = optional_decimal("price_column", "price")
        fee = optional_decimal("fee_column", "fee")
        amount = optional_decimal("amount_column", "amount")

        errors.extend(row_errors)
        if row_errors or when is None or action is None:
            continue
        if action == "ignore":
            continue
        symbol = reader.optional_cell(row, symbol_index).upper() if symbol_index is not None else ""
        if action in ("buy", "sell", "vest"):
            if not symbol or quantity is None:
                errors.append(RowError(line_no, f"{action} rows need a symbol and a quantity", raw))
                continue
            if quantity == 0:
                errors.append(RowError(line_no, f"{action} rows need a non-zero quantity", raw))
                continue
            # Brokers sign the quantity by direction ("Sold ... -1.500"); the
            # action already carries the direction, so store the magnitude.
            quantity = abs(quantity)
        parsed.append(
            ParsedStockRow(
                line_no=line_no,
                date=when,
                action=action,
                symbol=symbol,
                quantity=quantity,
                price=price,
                fee=fee,
                amount=amount,
                description=reader.optional_cell(row, desc_index) if desc_index is not None else "",
            )
        )
    if errors:
        raise CsvValidationError(errors)
    return parsed
