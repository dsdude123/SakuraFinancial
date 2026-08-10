from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from budget_service.main import create_app


class FakeLedger:
    """Stands in for the ledger service. Tests declare per-month actuals and
    the bills accrual; the shapes match ledger's real API responses."""

    def __init__(self):
        self.actuals: dict[str, list[dict]] = {}
        self.accrual: dict = {"bills": [], "total_monthly_load": "0.00", "by_category": []}
        # 20 "Food" is a parent of 21 Groceries and 22 Dining Out, so tests can
        # exercise budgeting at either level.
        self.category_list: list[dict] = [
            {"id": 1, "name": "Rent", "kind": "expense", "parent_id": None, "active": True},
            {"id": 2, "name": "Groceries", "kind": "expense", "parent_id": None, "active": True},
            {"id": 10, "name": "Salary", "kind": "income", "parent_id": None, "active": True},
            {"id": 20, "name": "Food", "kind": "expense", "parent_id": None, "active": True},
            {"id": 21, "name": "Groceries", "kind": "expense", "parent_id": 20, "active": True},
            {"id": 22, "name": "Dining Out", "kind": "expense", "parent_id": 20, "active": True},
        ]

    def set_month(self, month: str, income: str = "0", spend: dict[int, str] | None = None):
        """spend maps category_id -> positive spent amount (net outflow)."""
        rows = []
        if Decimal(income) != 0:
            rows.append({"category_id": 10, "name": "Salary", "kind": "income", "parent_id": None, "net": income})
        for category_id, amount in (spend or {}).items():
            name = next(
                (c["name"] for c in self.category_list if c["id"] == category_id), "Unknown"
            )
            rows.append(
                {
                    "category_id": category_id,
                    "name": name,
                    "kind": "expense",
                    "parent_id": None,
                    "net": str(-Decimal(amount)),
                }
            )
        self.actuals[month] = rows

    def category_actuals(self, start: date, end: date) -> list[dict]:
        return self.actuals.get(f"{start.year:04d}-{start.month:02d}", [])

    def bills_accrual(self, on=None) -> dict:
        return self.accrual

    def categories(self) -> list[dict]:
        return self.category_list


@pytest.fixture()
def ledger():
    return FakeLedger()


@pytest.fixture()
def client(ledger):
    app = create_app(database_url="sqlite://", ledger_client=ledger)
    with TestClient(app) as test_client:
        yield test_client
