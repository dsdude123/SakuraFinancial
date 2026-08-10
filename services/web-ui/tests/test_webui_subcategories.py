"""End-to-end subcategories through the real stack: Food broken into
Groceries vs Dining Out, budgeted at either level."""

import pytest
from fastapi.testclient import TestClient

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@pytest.fixture()
def food_tree(stack):
    """Checking account + Food{Groceries, Dining Out} + August spending."""
    ledger = TestClient(stack["ledger"])
    ledger.post("/api/accounts", json={"name": "Checking", "type": "checking"})
    food = ledger.post("/api/categories", json={"name": "Food", "kind": "expense"}).json()
    groceries = ledger.post(
        "/api/categories",
        json={"name": "Groceries", "kind": "expense", "parent_id": food["id"]},
    ).json()
    dining = ledger.post(
        "/api/categories",
        json={"name": "Dining Out", "kind": "expense", "parent_id": food["id"]},
    ).json()
    for category_id, amount, day in (
        (groceries["id"], "-600.00", "2026-08-04"),
        (dining["id"], "-250.00", "2026-08-11"),
    ):
        ledger.post(
            "/api/transactions",
            json={
                "account_id": 1,
                "date": day,
                "splits": [{"category_id": category_id, "amount": amount}],
            },
        )
    return {"food": food, "groceries": groceries, "dining": dining, "ledger": ledger}


class TestCategoryAdmin:
    def test_create_subcategory_from_the_page(self, logged_in, stack):
        logged_in.post("/categories", data={"name": "Food", "kind": "expense", "parent_id": ""})
        logged_in.post(
            "/categories", data={"name": "Groceries", "kind": "expense", "parent_id": "1"}
        )
        page = logged_in.get("/categories")
        assert "Food" in page.text
        assert "&#8627; Groceries" in page.text  # indented under its parent

    def test_dropdowns_show_the_full_path(self, logged_in, food_tree):
        register = logged_in.get("/accounts/1/register")
        assert "Food: Groceries" in register.text
        assert "Food: Dining Out" in register.text


class TestSpendingReport:
    def test_parent_totals_with_children_indented(self, logged_in, food_tree):
        page = logged_in.get("/reports/spending?month=2026-08")
        assert "850.00" in page.text  # Food total
        assert "600.00" in page.text and "250.00" in page.text  # the breakdown
        assert "&#8627; Groceries" in page.text
        assert "break down:" in page.text  # drill-down link row

    def test_drill_down_chart_renders(self, logged_in, food_tree):
        food_id = food_tree["food"]["id"]
        page = logged_in.get(f"/reports/spending?month=2026-08&detail={food_id}")
        assert "back to all categories" in page.text
        chart = logged_in.get(f"/charts/spending.png?month=2026-08&detail={food_id}")
        assert chart.content.startswith(PNG_SIGNATURE)

    def test_top_level_chart_still_renders(self, logged_in, food_tree):
        chart = logged_in.get("/charts/spending.png?month=2026-08")
        assert chart.content.startswith(PNG_SIGNATURE)


class TestBudgetPage:
    def test_budget_at_parent_covers_children(self, logged_in, food_tree, stack):
        food_id = food_tree["food"]["id"]
        logged_in.post(
            "/budget/2026-08/set", data={"category_id": str(food_id), "amount": "800.00"}
        )
        view = TestClient(stack["budget"]).get("/api/budget/2026-08").json()
        food_row = next(r for r in view["categories"] if r["category_id"] == food_id)
        assert food_row["spent"] == "850.00"
        assert food_row["available"] == "-50.00"

        page = logged_in.get("/budget?month=2026-08")
        assert "&#8627; Groceries" in page.text
        assert "over" in page.text

    def test_budget_at_children_shows_parent_subtotal(self, logged_in, food_tree):
        logged_in.post(
            "/budget/2026-08/set",
            data={"category_id": str(food_tree["groceries"]["id"]), "amount": "500.00"},
        )
        logged_in.post(
            "/budget/2026-08/set",
            data={"category_id": str(food_tree["dining"]["id"]), "amount": "300.00"},
        )
        page = logged_in.get("/budget?month=2026-08")
        assert "subtotal" in page.text  # Food row becomes read-only
        assert "800.00" in page.text  # 500 + 300 summed on the parent

    def test_child_without_budget_shows_no_phantom_deficit(self, logged_in, food_tree):
        logged_in.post(
            "/budget/2026-08/set",
            data={"category_id": str(food_tree["food"]["id"]), "amount": "800.00"},
        )
        page = logged_in.get("/budget?month=2026-08")
        # the em-dash placeholder marks a detail row with no envelope of its own
        assert "&mdash;" in page.text


class TestSeedDefaults:
    def test_starter_set_is_nested(self, logged_in, stack):
        logged_in.post("/categories/seed-defaults")
        paths = [c["path"] for c in TestClient(stack["ledger"]).get("/api/categories").json()]
        assert "Food: Groceries" in paths
        assert "Food: Dining Out" in paths
        assert "Utilities: Electric" in paths
