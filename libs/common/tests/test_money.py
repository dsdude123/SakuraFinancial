from decimal import Decimal

import pytest

from sakura_common.money import AmountParseError, format_amount, parse_amount, quantize


class TestParseAmount:
    def test_plain(self):
        assert parse_amount("12.34") == Decimal("12.34")

    def test_negative_sign(self):
        assert parse_amount("-12.34") == Decimal("-12.34")

    def test_leading_plus(self):
        assert parse_amount("+7.00") == Decimal("7.00")

    def test_parentheses_negative(self):
        assert parse_amount("(1,234.56)") == Decimal("-1234.56")

    def test_thousands_and_symbol(self):
        assert parse_amount("$1,234.56") == Decimal("1234.56")

    def test_currency_code_suffix(self):
        assert parse_amount("55.00 CAD") == Decimal("55.00")

    def test_ntd_suffix(self):
        assert parse_amount("100 NTD") == Decimal("100")

    def test_empty_raises(self):
        with pytest.raises(AmountParseError, match="empty"):
            parse_amount("   ")

    def test_garbage_raises_with_original_text(self):
        with pytest.raises(AmountParseError, match="WITHDRAWAL"):
            parse_amount("WITHDRAWAL")


class TestFormatting:
    def test_quantize_default_two_decimals(self):
        assert quantize(Decimal("1.005")) == Decimal("1.01")

    def test_quantize_zero_decimal_currency(self):
        assert quantize(Decimal("100.4"), "JPY") == Decimal("100")

    def test_format_thousands(self):
        assert format_amount(Decimal("-1234.5"), "USD") == "-1,234.50"

    def test_format_with_code(self):
        assert format_amount(Decimal("20"), "CAD", with_code=True) == "20.00 CAD"
