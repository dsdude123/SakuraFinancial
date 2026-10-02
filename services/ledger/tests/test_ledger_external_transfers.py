"""Transfers whose other side isn't a ledger account.

Funding a brokerage is money moving, not money spent, but the far account
lives in the stocks service — so the ledger books one leg and records the ref
of the account on the other end. These tests pin the two things that made the
feature necessary: the balance moves, and no report ever sees it as spending.
"""


def external_transfer(client, account, **overrides):
    body = {
        "account_id": account["id"],
        "date": "2026-09-10",
        "amount": "2500.00",
        "direction": "out",
        "external_account": "stock:1",
        "external_name": "Fidelity",
    }
    body.update(overrides)
    return client.post("/api/transfers/external", json=body)


class TestBooking:
    def test_money_leaves_the_account(self, client, checking):
        response = external_transfer(client, checking)
        assert response.status_code == 200
        leg = response.json()
        assert leg["kind"] == "transfer"
        assert leg["total"] == "-2500.00"
        assert leg["external_account"] == "stock:1"
        assert leg["transfer_group_id"]  # the far side is found by this
        assert leg["splits"][0]["category_id"] is None
        assert client.get(f"/api/accounts/{checking['id']}").json()["balance"] == "-1500.00"

    def test_money_coming_back_from_the_brokerage(self, client, checking):
        response = external_transfer(client, checking, direction="in", amount="400.00")
        assert response.status_code == 200
        assert response.json()["total"] == "400.00"
        assert client.get(f"/api/accounts/{checking['id']}").json()["balance"] == "1400.00"

    def test_only_one_leg_is_written(self, client, checking):
        external_transfer(client, checking)
        txns = client.get("/api/transactions").json()
        assert len(txns) == 1

    def test_the_memo_names_the_far_account(self, client, checking):
        out = external_transfer(client, checking).json()
        assert out["memo"] == "Transfer to Fidelity"
        back = external_transfer(client, checking, direction="in").json()
        assert back["memo"] == "Transfer from Fidelity"

    def test_a_given_memo_wins(self, client, checking):
        leg = external_transfer(client, checking, memo="Monthly investing").json()
        assert leg["memo"] == "Monthly investing"

    def test_the_caller_can_supply_the_group_id(self, client, checking):
        """The other service may have written its row first."""
        leg = external_transfer(client, checking, transfer_group_id="abc-123").json()
        assert leg["transfer_group_id"] == "abc-123"


class TestRefusals:
    def test_a_negative_amount(self, client, checking):
        assert external_transfer(client, checking, amount="-5").status_code == 422

    def test_zero(self, client, checking):
        assert external_transfer(client, checking, amount="0").status_code == 422

    def test_an_unknown_direction(self, client, checking):
        assert external_transfer(client, checking, direction="sideways").status_code == 422

    def test_a_missing_far_account(self, client, checking):
        assert external_transfer(client, checking, external_account=" ").status_code == 422

    def test_an_unknown_account(self, client):
        response = client.post(
            "/api/transfers/external",
            json={
                "account_id": 999,
                "date": "2026-09-10",
                "amount": "10",
                "external_account": "stock:1",
            },
        )
        assert response.status_code == 422


class TestReportsIgnoreIt:
    """The whole point: before this existed the only way to record funding a
    brokerage was to invent an expense category, which then sat in every
    spending report and budget forever."""

    def test_it_is_not_spending(self, client, checking, rent_category):
        client.post(
            "/api/transactions",
            json={
                "account_id": checking["id"],
                "date": "2026-09-05",
                "splits": [{"category_id": rent_category["id"], "amount": "-1200.00"}],
            },
        )
        external_transfer(client, checking)
        tree = client.get(
            "/api/reports/category-tree", params={"start": "2026-09-01", "end": "2026-09-30"}
        ).json()
        assert [(node["name"], node["net"]) for node in tree] == [("Rent", "-1200.00")]

    def test_it_is_not_in_cash_flow(self, client, checking, rent_category):
        client.post(
            "/api/transactions",
            json={
                "account_id": checking["id"],
                "date": "2026-09-05",
                "splits": [{"category_id": rent_category["id"], "amount": "-1200.00"}],
            },
        )
        external_transfer(client, checking)
        september = next(
            row
            for row in client.get("/api/reports/cashflow", params={"months": 24}).json()
            if row["month"] == "2026-09"
        )
        assert september["spending"] == "1200.00"

    def test_but_net_worth_sees_the_money_go(self, client, checking):
        before = client.get("/api/reports/net-worth", params={"months": 1}).json()[-1]
        external_transfer(client, checking)
        after = client.get("/api/reports/net-worth", params={"months": 1}).json()[-1]
        # The ledger is poorer by the transfer; the stocks service reports the
        # other half, and the net-worth page adds the two together.
        from decimal import Decimal

        assert Decimal(after["total"]) == Decimal(before["total"]) - Decimal("2500.00")


class TestDeleting:
    def test_delete_reports_the_far_side_so_it_can_be_cleaned_up(self, client, checking):
        leg = external_transfer(client, checking).json()
        result = client.delete(f"/api/transactions/{leg['id']}").json()
        assert result["deleted"] == [leg["id"]]
        assert result["external_account"] == "stock:1"
        assert result["transfer_group_id"] == leg["transfer_group_id"]
        assert client.get(f"/api/accounts/{checking['id']}").json()["balance"] == "1000.00"

    def test_an_ordinary_transaction_has_no_far_side(self, client, checking, rent_category):
        txn = client.post(
            "/api/transactions",
            json={
                "account_id": checking["id"],
                "date": "2026-09-05",
                "splits": [{"category_id": rent_category["id"], "amount": "-10.00"}],
            },
        ).json()
        result = client.delete(f"/api/transactions/{txn['id']}").json()
        assert result["external_account"] is None


def test_a_backup_round_trip_keeps_the_link(client, checking):
    leg = external_transfer(client, checking).json()
    export = client.get("/api/export").json()
    assert export["transactions"][0]["external_account"] == "stock:1"

    client.post("/api/import", json=export)
    restored = client.get(f"/api/transactions/{leg['id']}").json()
    assert restored["external_account"] == "stock:1"
    assert restored["transfer_group_id"] == leg["transfer_group_id"]
