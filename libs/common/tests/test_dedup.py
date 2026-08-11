from datetime import date
from decimal import Decimal

from sakura_common.dedup import normalize_description, row_hash


def test_normalize_collapses_whitespace_and_uppercases():
    assert normalize_description("  Amazon   mktp  US ") == "AMAZON MKTP US"


def test_hash_stable_across_cosmetic_differences():
    a = row_hash(1, date(2026, 8, 1), Decimal("-12.34"), "AMAZON  MKTP US")
    b = row_hash(1, date(2026, 8, 1), Decimal("-12.34"), " amazon mktp us")
    assert a == b


def test_hash_differs_by_account_date_amount_description():
    base = row_hash(1, date(2026, 8, 1), Decimal("-12.34"), "STORE")
    assert row_hash(2, date(2026, 8, 1), Decimal("-12.34"), "STORE") != base
    assert row_hash(1, date(2026, 8, 2), Decimal("-12.34"), "STORE") != base
    assert row_hash(1, date(2026, 8, 1), Decimal("-12.35"), "STORE") != base
    assert row_hash(1, date(2026, 8, 1), Decimal("-12.34"), "OTHER") != base
