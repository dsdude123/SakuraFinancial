from datetime import date
from decimal import Decimal

import pytest

from sakura_common.csvengine import (
    CsvValidationError,
    parse_bank_csv,
    parse_stock_csv,
)

SIMPLE_PROFILE = {
    "date_column": "date",
    "date_format": "%m/%d/%Y",
    "description_column": "description",
    "amount_column": "amount",
}

SIMPLE_CSV = """Date,Description,Amount
08/01/2026,ACME PAYROLL,\"5,000.00\"
08/02/2026,AMAZON MKTP US,-45.10
"""


class TestBankSimple:
    def test_parses_rows(self):
        rows = parse_bank_csv(SIMPLE_CSV, SIMPLE_PROFILE)
        assert len(rows) == 2
        assert rows[0].date == date(2026, 8, 1)
        assert rows[0].amount == Decimal("5000.00")
        assert rows[1].description == "AMAZON MKTP US"
        assert rows[1].amount == Decimal("-45.10")

    def test_negate_amount_for_charge_positive_banks(self):
        rows = parse_bank_csv(
            "Date,Description,Amount\n08/02/2026,STORE,45.10\n",
            {**SIMPLE_PROFILE, "negate_amount": True},
        )
        assert rows[0].amount == Decimal("-45.10")

    def test_headerless_indexed_columns(self):
        rows = parse_bank_csv(
            "08/02/2026;STORE;-9.99\n",
            {
                "has_header": False,
                "delimiter": ";",
                "date_column": 0,
                "description_column": 1,
                "amount_column": 2,
            },
        )
        assert rows[0].amount == Decimal("-9.99")

    def test_skip_top_rows(self):
        text = "Account: 1234\nSome bank banner\nDate,Description,Amount\n08/02/2026,STORE,-1.00\n"
        rows = parse_bank_csv(text, {**SIMPLE_PROFILE, "skip_top_rows": 2})
        assert len(rows) == 1

    def test_blank_lines_ignored(self):
        rows = parse_bank_csv(SIMPLE_CSV + "\n\n", SIMPLE_PROFILE)
        assert len(rows) == 2


class TestBankDebitCredit:
    PROFILE = {
        "date_column": "date",
        "description_column": "payee",
        "amount_mode": "debit_credit",
        "debit_column": "debit",
        "credit_column": "credit",
    }

    def test_debit_negative_credit_positive(self):
        text = "Date,Payee,Debit,Credit\n08/01/2026,STORE,12.34,\n08/02/2026,EMPLOYER,,900.00\n"
        rows = parse_bank_csv(text, self.PROFILE)
        assert rows[0].amount == Decimal("-12.34")
        assert rows[1].amount == Decimal("900.00")

    def test_both_filled_is_error(self):
        text = "Date,Payee,Debit,Credit\n08/01/2026,STORE,1.00,2.00\n"
        with pytest.raises(CsvValidationError) as exc:
            parse_bank_csv(text, self.PROFILE)
        assert "both a debit and a credit" in exc.value.errors[0].message

    def test_neither_filled_is_error(self):
        text = "Date,Payee,Debit,Credit\n08/01/2026,STORE,,\n"
        with pytest.raises(CsvValidationError):
            parse_bank_csv(text, self.PROFILE)


class TestAllOrNothing:
    def test_all_errors_reported_and_nothing_returned(self):
        text = (
            "Date,Description,Amount\n"
            "08/01/2026,GOOD ROW,-1.00\n"
            "08/02/2026,BAD AMOUNT,not-money\n"
            "2026-08-03,BAD DATE,-2.00\n"
        )
        with pytest.raises(CsvValidationError) as exc:
            parse_bank_csv(text, SIMPLE_PROFILE)
        messages = [e.message for e in exc.value.errors]
        assert len(messages) == 2
        assert any("not a number" in m for m in messages)
        assert any("does not match format" in m for m in messages)
        # line numbers point at the actual file lines
        assert [e.line_no for e in exc.value.errors] == [3, 4]

    def test_missing_column_reported_with_available_columns(self):
        with pytest.raises(CsvValidationError) as exc:
            parse_bank_csv(
                "Datum,Description,Amount\n08/01/2026,STORE,-1.00\n", SIMPLE_PROFILE
            )
        assert "'date'" in exc.value.errors[0].message
        assert "datum" in exc.value.errors[0].message

    def test_empty_file_is_error(self):
        with pytest.raises(CsvValidationError):
            parse_bank_csv("Date,Description,Amount\n", SIMPLE_PROFILE)


STOCK_PROFILE = {
    "date_column": "date",
    "date_format": "%m/%d/%Y",
    "action_column": "type",
    "symbol_column": "symbol",
    "quantity_column": "qty",
    "price_column": "price",
    "amount_column": "amount",
    "action_map": {"Bought": "buy", "YOU SOLD": "sell", "DIV": "dividend", "WIRE IN": "deposit"},
}


class TestStockCsv:
    def test_action_map_translates_broker_strings(self):
        text = (
            "Date,Type,Symbol,Qty,Price,Amount\n"
            "08/01/2026,Bought,AAPL,10,150.00,-1500.00\n"
            "08/02/2026,YOU SOLD,MSFT,5,300.00,1500.00\n"
            "08/03/2026,WIRE IN,,,,2000.00\n"
        )
        rows = parse_stock_csv(text, STOCK_PROFILE)
        assert [r.action for r in rows] == ["buy", "sell", "deposit"]
        assert rows[0].symbol == "AAPL"
        assert rows[0].quantity == Decimal("10")

    def test_unmapped_action_is_a_row_error(self):
        text = "Date,Type,Symbol,Qty,Price,Amount\n08/01/2026,Reinvest,AAPL,1,150.00,-150.00\n"
        with pytest.raises(CsvValidationError) as exc:
            parse_stock_csv(text, STOCK_PROFILE)
        assert "no mapping" in exc.value.errors[0].message
        assert "Reinvest" in exc.value.errors[0].message

    def test_action_map_is_case_insensitive(self):
        text = "Date,Type,Symbol,Qty,Price,Amount\n08/01/2026,BOUGHT,AAPL,1,150.00,-150.00\n"
        rows = parse_stock_csv(text, STOCK_PROFILE)
        assert rows[0].action == "buy"

    def test_ignore_action_drops_row(self):
        profile = {**STOCK_PROFILE, "action_map": {**STOCK_PROFILE["action_map"], "Journal": "ignore"}}
        text = (
            "Date,Type,Symbol,Qty,Price,Amount\n"
            "08/01/2026,Journal,,,,0.00\n"
            "08/02/2026,Bought,AAPL,1,150.00,-150.00\n"
        )
        rows = parse_stock_csv(text, profile)
        assert len(rows) == 1

    def test_buy_without_symbol_is_error(self):
        text = "Date,Type,Symbol,Qty,Price,Amount\n08/01/2026,Bought,,1,150.00,-150.00\n"
        with pytest.raises(CsvValidationError) as exc:
            parse_stock_csv(text, STOCK_PROFILE)
        assert "need a symbol" in exc.value.errors[0].message

    def test_bad_map_target_is_error(self):
        profile = {**STOCK_PROFILE, "action_map": {"Bought": "purchase"}}
        text = "Date,Type,Symbol,Qty,Price,Amount\n08/01/2026,Bought,AAPL,1,150.00,-150.00\n"
        with pytest.raises(CsvValidationError) as exc:
            parse_stock_csv(text, profile)
        assert "unknown action" in exc.value.errors[0].message


class TestBrokerPreambleAndPlaceholders:
    """Real broker exports open with a few summary lines separated by blank
    ones, and write a placeholder where a column doesn't apply to a row."""

    def test_skip_top_rows_ignores_blank_lines(self):
        text = (
            "All Transactions Activity Types\n"
            "\n"
            "Account Activity for Individual Brokerage -0000\n"
            "\n"
            "Total:,0.00\n"
            "\n"
            "Date,Type,Symbol,Qty,Price,Amount\n"
            "08/01/2026,Bought,AAPL,1,150.00,-150.00\n"
        )
        # Three visible junk lines; the user never counts the blanks between.
        rows = parse_stock_csv(text, {**STOCK_PROFILE, "skip_top_rows": 3})
        assert len(rows) == 1
        assert rows[0].symbol == "AAPL"
        # Errors still point at the true line in the file.
        assert rows[0].line_no == 8

    def test_placeholder_cells_read_as_empty(self):
        text = (
            "Date,Type,Symbol,Qty,Price,Amount\n"
            "08/01/2026,WIRE IN,--,,,1000.00\n"
            "08/02/2026,DIV,AAPL,N/A,--,5.00\n"
        )
        rows = parse_stock_csv(text, STOCK_PROFILE)
        assert rows[0].symbol == ""  # not a ticker called "--"
        assert rows[1].symbol == "AAPL"
        assert rows[1].quantity is None
        assert rows[1].price is None

    def test_null_tokens_are_configurable(self):
        text = "Date,Type,Symbol,Qty,Price,Amount\n08/01/2026,WIRE IN,NIL,,,1000.00\n"
        rows = parse_stock_csv(text, {**STOCK_PROFILE, "null_tokens": ["nil"]})
        assert rows[0].symbol == ""

    def test_sale_quantity_is_normalized_to_a_magnitude(self):
        text = "Date,Type,Symbol,Qty,Price,Amount\n08/01/2026,YOU SOLD,AAPL,-1.500,50.00,75.00\n"
        rows = parse_stock_csv(text, STOCK_PROFILE)
        assert rows[0].action == "sell"
        assert rows[0].quantity == Decimal("1.5")

    def test_zero_quantity_trade_is_a_row_error(self):
        text = "Date,Type,Symbol,Qty,Price,Amount\n08/01/2026,Bought,AAPL,0,150.00,-150.00\n"
        with pytest.raises(CsvValidationError) as exc:
            parse_stock_csv(text, STOCK_PROFILE)
        assert "non-zero quantity" in exc.value.errors[0].message
