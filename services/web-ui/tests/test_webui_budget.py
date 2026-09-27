"""The budget page as the user drives it: set a figure once and it holds for
every later month, and there is a visible way to take a category back off."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def budget_api(stack):
    return TestClient(stack["budget"])


@pytest.fixture()
def categories(stack):
    ledger = TestClient(stack["ledger"])
    ledger.post("/api/accounts", json={"name": "Checking", "type": "checking"})
    rent = ledger.post("/api/categories", json={"name": "Rent", "kind": "expense"}).json()
    fuel = ledger.post("/api/categories", json={"name": "Fuel", "kind": "expense"}).json()
    return {"rent": rent, "fuel": fuel, "ledger": ledger}


def row_for(budget_api, month, category_id):
    view = budget_api.get(f"/api/budget/{month}").json()
    return next((r for r in view["categories"] if r["category_id"] == category_id), None)


class TestSetOnceHoldsForever:
    def test_a_budget_set_in_one_month_shows_in_later_months(
        self, logged_in, categories, budget_api
    ):
        rent = categories["rent"]["id"]
        logged_in.post(
            "/budget/2026-03/set", data={"category_id": str(rent), "amount": "2000.00"}
        )
        for month in ("2026-04", "2026-07", "2027-02"):
            assert row_for(budget_api, month, rent)["budgeted"] == "2000.00"

    def test_the_page_says_where_a_carried_amount_came_from(self, logged_in, categories):
        rent = categories["rent"]["id"]
        logged_in.post(
            "/budget/2026-03/set", data={"category_id": str(rent), "amount": "2000.00"}
        )
        page = logged_in.get("/budget?month=2026-07")
        assert "carried forward from 2026-03" in page.text
        # And it is not offered again as a category to add.
        assert page.text.count(">Rent<") <= 1

    def test_changing_a_later_month_leaves_earlier_ones_alone(
        self, logged_in, categories, budget_api
    ):
        rent = categories["rent"]["id"]
        logged_in.post(
            "/budget/2026-03/set", data={"category_id": str(rent), "amount": "2000.00"}
        )
        logged_in.post(
            "/budget/2026-06/set", data={"category_id": str(rent), "amount": "2200.00"}
        )
        assert row_for(budget_api, "2026-05", rent)["budgeted"] == "2000.00"
        assert row_for(budget_api, "2026-09", rent)["budgeted"] == "2200.00"

    def test_the_copy_last_month_button_is_gone(self, logged_in, categories):
        page = logged_in.get("/budget?month=2026-08")
        assert "Copy last month" not in page.text
        assert "every month after it" in page.text


class TestRemovingACategory:
    def test_the_page_offers_a_remove_button(self, logged_in, categories):
        rent = categories["rent"]["id"]
        logged_in.post(
            "/budget/2026-03/set", data={"category_id": str(rent), "amount": "2000.00"}
        )
        page = logged_in.get("/budget?month=2026-03")
        assert 'value="Remove"' in page.text
        assert f"/budget/2026-03/remove" in page.text

    def test_remove_takes_it_off_from_that_month_on(self, logged_in, categories, budget_api):
        rent = categories["rent"]["id"]
        logged_in.post(
            "/budget/2026-03/set", data={"category_id": str(rent), "amount": "2000.00"}
        )
        response = logged_in.post(
            "/budget/2026-06/remove", data={"category_id": str(rent)}, follow_redirects=False
        )
        assert response.status_code == 303
        assert row_for(budget_api, "2026-05", rent)["budgeted"] == "2000.00"
        assert row_for(budget_api, "2026-06", rent) is None
        assert row_for(budget_api, "2027-01", rent) is None

    def test_a_removed_category_is_offered_again_for_adding(self, logged_in, categories):
        rent = categories["rent"]["id"]
        logged_in.post(
            "/budget/2026-03/set", data={"category_id": str(rent), "amount": "2000.00"}
        )
        logged_in.post("/budget/2026-06/remove", data={"category_id": str(rent)})
        page = logged_in.get("/budget?month=2026-06")
        assert f'<option value="{rent}">Rent</option>' in page.text

    def test_undo_restores_the_inherited_amount(self, logged_in, categories, budget_api):
        rent = categories["rent"]["id"]
        logged_in.post(
            "/budget/2026-03/set", data={"category_id": str(rent), "amount": "2000.00"}
        )
        logged_in.post("/budget/2026-06/remove", data={"category_id": str(rent)})
        assert row_for(budget_api, "2026-06", rent) is None

        response = logged_in.post(
            "/budget/2026-06/revert", data={"category_id": str(rent)}, follow_redirects=False
        )
        assert response.status_code == 303
        assert row_for(budget_api, "2026-06", rent)["budgeted"] == "2000.00"

    def test_undo_is_only_offered_where_a_change_was_made(self, logged_in, categories):
        rent = categories["rent"]["id"]
        logged_in.post(
            "/budget/2026-03/set", data={"category_id": str(rent), "amount": "2000.00"}
        )
        # The form action, not the label — the help text mentions the wording too.
        assert "/budget/2026-03/revert" in logged_in.get("/budget?month=2026-03").text
        assert "/budget/2026-07/revert" not in logged_in.get("/budget?month=2026-07").text

    def test_undoing_where_nothing_changed_explains_itself(self, logged_in, categories):
        rent = categories["rent"]["id"]
        logged_in.post(
            "/budget/2026-03/set", data={"category_id": str(rent), "amount": "2000.00"}
        )
        response = logged_in.post(
            "/budget/2026-07/revert", data={"category_id": str(rent)}, follow_redirects=False
        )
        assert response.status_code == 303
        assert "err=" in response.headers["location"]


class TestCarriedBudgetsAreReal:
    def test_a_month_never_visited_still_tracks_overspending(
        self, logged_in, categories, budget_api
    ):
        """The behaviour that matters: carrying forward isn't a display trick,
        it drives deficits in months the user never opened."""
        rent = categories["rent"]["id"]
        logged_in.post(
            "/budget/2026-03/set", data={"category_id": str(rent), "amount": "100.00"}
        )
        categories["ledger"].post(
            "/api/transactions",
            json={
                "account_id": 1,
                "date": "2026-04-10",
                "splits": [{"category_id": rent, "amount": "-150.00"}],
            },
        )
        april = row_for(budget_api, "2026-04", rent)
        assert april["budgeted"] == "100.00"
        assert april["over"] is True
        assert row_for(budget_api, "2026-05", rent)["carry_in"] == "-50.00"
