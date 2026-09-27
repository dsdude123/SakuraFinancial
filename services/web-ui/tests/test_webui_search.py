"""The search page and bulk editing, driven the way the user drives them."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def ledger_api(stack):
    return TestClient(stack["ledger"])


@pytest.fixture()
def seeded(logged_in, ledger_api):
    ledger_api.post("/api/accounts", json={"name": "Checking", "type": "checking"})
    rent = ledger_api.post("/api/categories", json={"name": "Rent"}).json()
    food = ledger_api.post("/api/categories", json={"name": "Food"}).json()
    payee = ledger_api.post("/api/payees", json={"name": "Cafe"}).json()
    ids = []
    for day, amount in (("2026-08-01", "-4.50"), ("2026-08-02", "-5.25"), ("2026-08-03", "-3.75")):
        ids.append(
            ledger_api.post(
                "/api/transactions",
                json={
                    "account_id": 1,
                    "date": day,
                    "payee_id": payee["id"],
                    "memo": "flat white",
                    "splits": [{"category_id": rent["id"], "amount": amount}],
                },
            ).json()["id"]
        )
    return {"ids": ids, "rent": rent, "food": food, "payee": payee, "api": ledger_api}


class TestSearchPage:
    def test_it_is_in_the_nav(self, logged_in):
        assert 'href="/search"' in logged_in.get("/").text

    def test_an_unsearched_page_prompts_rather_than_listing_everything(self, logged_in):
        page = logged_in.get("/search")
        assert "Pick some filters" in page.text

    def test_text_search_finds_by_memo(self, logged_in, seeded):
        page = logged_in.get("/search?q=flat white")
        assert "3 matches" in page.text
        assert "flat white" in page.text

    def test_filtering_by_category(self, logged_in, seeded):
        page = logged_in.get(f"/search?category_id={seeded['rent']['id']}")
        assert "3 matches" in page.text
        page = logged_in.get(f"/search?category_id={seeded['food']['id']}")
        assert "Nothing matched" in page.text

    def test_uncategorized_filter(self, logged_in, seeded):
        seeded["api"].post(
            "/api/transactions",
            json={
                "account_id": 1,
                "date": "2026-08-09",
                "splits": [{"category_id": None, "amount": "-9.99"}],
            },
        )
        page = logged_in.get("/search?uncategorized=on")
        assert "1 match" in page.text

    def test_date_range(self, logged_in, seeded):
        page = logged_in.get("/search?start=2026-08-02&end=2026-08-02")
        assert "1 match" in page.text


class TestBulkEditFromThepage:
    def test_recategorizing_a_selection(self, logged_in, seeded):
        response = logged_in.post(
            "/search/bulk",
            data={
                "txn": [str(i) for i in seeded["ids"]],
                "apply_category_id": str(seeded["food"]["id"]),
                "q": "flat white",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "Updated%203" in response.headers["location"]
        # And it comes back to the same search.
        assert response.headers["location"].startswith("/search?q=flat%20white")
        assert "category_id" not in response.headers["location"]  # filters, not the edit

        for txn in seeded["api"].get("/api/transactions").json():
            assert txn["splits"][0]["category_id"] == seeded["food"]["id"]

    def test_nothing_ticked_is_explained(self, logged_in, seeded):
        response = logged_in.post(
            "/search/bulk",
            data={"apply_category_id": str(seeded["food"]["id"])},
            follow_redirects=False,
        )
        assert "err=" in response.headers["location"]
        assert "Tick%20the%20transactions" in response.headers["location"]

    def test_no_change_chosen_is_explained(self, logged_in, seeded):
        response = logged_in.post(
            "/search/bulk",
            data={"txn": [str(seeded["ids"][0])]},
            follow_redirects=False,
        )
        assert "err=" in response.headers["location"]

    def test_skipped_rows_are_reported_not_hidden(self, logged_in, seeded):
        split = seeded["api"].post(
            "/api/transactions",
            json={
                "account_id": 1,
                "date": "2026-08-06",
                "splits": [
                    {"category_id": seeded["rent"]["id"], "amount": "-30.00"},
                    {"category_id": seeded["food"]["id"], "amount": "-20.00"},
                ],
            },
        ).json()
        response = logged_in.post(
            "/search/bulk",
            data={
                "txn": [str(i) for i in seeded["ids"]] + [str(split["id"])],
                "apply_category_id": str(seeded["food"]["id"]),
            },
            follow_redirects=False,
        )
        location = response.headers["location"]
        assert "Updated%203" in location
        assert "left%20alone" in location

    def test_the_page_offers_the_bulk_controls_with_results(self, logged_in, seeded):
        page = logged_in.get("/search?q=flat white")
        assert "Change the ticked rows" in page.text
        assert 'name="txn"' in page.text
        assert 'value="Apply to ticked"' in page.text
