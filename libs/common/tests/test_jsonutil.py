from datetime import date, datetime
from decimal import Decimal

from sakura_common import jsonutil


def test_decimal_round_trip():
    text = jsonutil.dumps({"amount": Decimal("1234.50")})
    assert '"1234.50"' in text
    assert jsonutil.parse_decimal(jsonutil.loads(text)["amount"]) == Decimal("1234.50")


def test_date_round_trip():
    text = jsonutil.dumps({"when": date(2026, 8, 9)})
    assert jsonutil.parse_date(jsonutil.loads(text)["when"]) == date(2026, 8, 9)


def test_datetime_round_trip():
    stamp = datetime(2026, 8, 9, 12, 30, 0)
    text = jsonutil.dumps({"at": stamp})
    assert jsonutil.parse_datetime(jsonutil.loads(text)["at"]) == stamp


def test_none_parses_to_none():
    assert jsonutil.parse_decimal(None) is None
    assert jsonutil.parse_date("") is None


def test_sorted_keys_for_clean_diffs():
    assert jsonutil.dumps({"b": 1, "a": 2}, indent=None) == '{"a": 2, "b": 1}'
