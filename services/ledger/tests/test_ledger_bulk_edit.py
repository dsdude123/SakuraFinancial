"""Bulk editing a set of transactions — the alternative to re-filing a year of
imported rows one at a time."""

import pytest


@pytest.fixture()
def coffee(client, checking, rent_category):
    """Three transactions on one category, plus a split and a transfer."""
    ids = []
    for day, amount in (("2026-08-01", "-4.50"), ("2026-08-02", "-5.25"), ("2026-08-03", "-3.75")):
        ids.append(
            client.post(
                "/api/transactions",
                json={
                    "account_id": checking["id"],
                    "date": day,
                    "splits": [{"category_id": rent_category["id"], "amount": amount}],
                },
            ).json()["id"]
        )
    return ids


def bulk(client, **body):
    return client.post("/api/transactions/bulk", json=body)


class TestBulkRecategorize:
    def test_many_transactions_take_a_new_category_at_once(
        self, client, coffee, groceries_category
    ):
        result = bulk(client, transaction_ids=coffee, category_id=groceries_category["id"]).json()
        assert result["updated"] == 3
        assert result["skipped"] == []
        for txn in client.get("/api/transactions").json():
            assert txn["splits"][0]["category_id"] == groceries_category["id"]

    def test_payee_and_status_can_be_set_too(self, client, coffee):
        payee = client.post("/api/payees", json={"name": "Coffee Shop"}).json()
        bulk(client, transaction_ids=coffee, payee_id=payee["id"], status="reconciled")
        for txn in client.get("/api/transactions").json():
            assert txn["payee_name"] == "Coffee Shop"
            assert txn["status"] == "reconciled"

    def test_a_split_transaction_is_left_alone_and_reported(
        self, client, checking, rent_category, groceries_category
    ):
        """Collapsing several categories into one would destroy how the money
        was actually divided, so it is refused rather than guessed at."""
        split = client.post(
            "/api/transactions",
            json={
                "account_id": checking["id"],
                "date": "2026-08-04",
                "splits": [
                    {"category_id": rent_category["id"], "amount": "-30.00"},
                    {"category_id": groceries_category["id"], "amount": "-20.00"},
                ],
            },
        ).json()
        result = bulk(
            client, transaction_ids=[split["id"]], category_id=groceries_category["id"]
        ).json()
        assert result["updated"] == 0
        assert result["skipped"][0]["id"] == split["id"]
        assert "split across 2" in result["skipped"][0]["reason"]

        after = client.get(f"/api/transactions/{split['id']}").json()
        assert [s["category_id"] for s in after["splits"]] == [
            rent_category["id"],
            groceries_category["id"],
        ]

    def test_a_transfer_is_left_alone_and_reported(
        self, client, checking, savings, groceries_category
    ):
        client.post(
            "/api/transfers",
            json={
                "from_account_id": checking["id"],
                "to_account_id": savings["id"],
                "date": "2026-08-05",
                "amount": "100.00",
            },
        )
        leg = next(t for t in client.get("/api/transactions").json() if t["kind"] == "transfer")
        result = bulk(
            client, transaction_ids=[leg["id"]], category_id=groceries_category["id"]
        ).json()
        assert result["updated"] == 0
        assert "transfer" in result["skipped"][0]["reason"]

    def test_a_mixed_selection_does_what_it_can_and_says_what_it_skipped(
        self, client, coffee, checking, rent_category, groceries_category
    ):
        split = client.post(
            "/api/transactions",
            json={
                "account_id": checking["id"],
                "date": "2026-08-04",
                "splits": [
                    {"category_id": rent_category["id"], "amount": "-30.00"},
                    {"category_id": groceries_category["id"], "amount": "-20.00"},
                ],
            },
        ).json()
        result = bulk(
            client,
            transaction_ids=coffee + [split["id"], 9999],
            category_id=groceries_category["id"],
        ).json()
        assert result["updated"] == 3
        reasons = {entry["id"]: entry["reason"] for entry in result["skipped"]}
        assert "split across 2" in reasons[split["id"]]
        assert reasons[9999] == "it no longer exists"


class TestBulkValidation:
    def test_an_empty_selection_is_rejected(self, client):
        assert bulk(client, transaction_ids=[]).status_code == 422

    def test_asking_for_no_change_is_rejected(self, client, coffee):
        response = bulk(client, transaction_ids=coffee)
        assert response.status_code == 422
        assert "Choose a category" in response.json()["detail"]

    def test_unknown_category_is_rejected_before_anything_changes(self, client, coffee):
        assert bulk(client, transaction_ids=coffee, category_id=9999).status_code == 422

    def test_unknown_status_is_rejected(self, client, coffee):
        assert bulk(client, transaction_ids=coffee, status="pending").status_code == 422
