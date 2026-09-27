"""The investment side of a bank transfer.

Cash arriving from checking is an ordinary deposit plus a link back to the
ledger leg that sent it: same group id, and the bank account's ref. The link
is what lets either side delete the other, so a transfer never survives as
half of itself.
"""


def transfer(client, account, **overrides):
    body = {
        "account_id": account["id"],
        "date": "2026-09-10",
        "amount": "2500.00",
        "direction": "in",
        "external_account": "bank:3",
        "external_name": "Checking",
        "transfer_group_id": "group-1",
    }
    body.update(overrides)
    return client.post("/api/transfers/external", json=body)


class TestBooking:
    def test_cash_arrives_as_a_linked_deposit(self, client, brokerage):
        response = transfer(client, brokerage)
        assert response.status_code == 200
        txn = response.json()
        assert txn["type"] == "deposit"
        assert txn["amount"] == "2500.00"
        assert txn["external_account"] == "bank:3"
        assert txn["transfer_group_id"] == "group-1"
        assert txn["note"] == "Transfer from Checking"
        assert client.get(f"/api/accounts/{brokerage['id']}").json()["cash"] == "12500.00"

    def test_cash_going_back_is_a_linked_withdrawal(self, client, brokerage):
        txn = transfer(client, brokerage, direction="out", amount="1000.00").json()
        assert txn["type"] == "withdraw"
        assert txn["amount"] == "-1000.00"
        assert txn["note"] == "Transfer to Checking"
        assert client.get(f"/api/accounts/{brokerage['id']}").json()["cash"] == "9000.00"

    def test_a_given_note_wins(self, client, brokerage):
        assert transfer(client, brokerage, note="Monthly investing").json()["note"] == (
            "Monthly investing"
        )

    def test_an_unlinked_deposit_keeps_both_fields_empty(self, client, brokerage):
        """Cash that really did appear out of nowhere — an employer paying
        straight into the brokerage — is still a plain deposit."""
        txn = client.post(
            "/api/transactions",
            json={
                "account_id": brokerage["id"],
                "type": "deposit",
                "date": "2026-09-10",
                "amount": "100.00",
            },
        ).json()
        assert txn["transfer_group_id"] is None
        assert txn["external_account"] is None


class TestRefusals:
    def test_a_negative_amount(self, client, brokerage):
        assert transfer(client, brokerage, amount="-1").status_code == 422

    def test_zero(self, client, brokerage):
        assert transfer(client, brokerage, amount="0").status_code == 422

    def test_an_unknown_direction(self, client, brokerage):
        assert transfer(client, brokerage, direction="sideways").status_code == 422

    def test_an_unknown_account(self, client):
        response = client.post(
            "/api/transfers/external",
            json={
                "account_id": 999,
                "date": "2026-09-10",
                "amount": "10",
                "external_account": "bank:3",
                "transfer_group_id": "group-1",
            },
        )
        assert response.status_code == 404

    def test_the_group_id_is_required(self, client, brokerage):
        """Without it the two halves could never find each other."""
        response = client.post(
            "/api/transfers/external",
            json={
                "account_id": brokerage["id"],
                "date": "2026-09-10",
                "amount": "10",
                "external_account": "bank:3",
            },
        )
        assert response.status_code == 422


class TestDeletingByGroup:
    def test_the_far_side_can_remove_this_one(self, client, brokerage):
        transfer(client, brokerage)
        result = client.delete("/api/transfers/external/group-1").json()
        assert len(result["deleted"]) == 1
        assert client.get(f"/api/accounts/{brokerage['id']}").json()["cash"] == "10000.00"
        assert client.get("/api/transactions").json() == []

    def test_an_unknown_group_is_not_an_error(self, client, brokerage):
        """Whoever deletes a leg calls the other side without knowing whether
        there is anything there — a restore, say, may have left only one."""
        response = client.delete("/api/transfers/external/nope")
        assert response.status_code == 200
        assert response.json()["deleted"] == []

    def test_other_activity_is_left_alone(self, client, brokerage):
        transfer(client, brokerage)
        client.post(
            "/api/transactions",
            json={
                "account_id": brokerage["id"],
                "type": "buy",
                "date": "2026-09-11",
                "symbol": "AAPL",
                "quantity": "10",
                "price": "150.00",
            },
        )
        client.delete("/api/transfers/external/group-1")
        remaining = client.get("/api/transactions").json()
        assert [t["type"] for t in remaining] == ["buy"]


def test_a_backup_round_trip_keeps_the_link(client, brokerage):
    transfer(client, brokerage)
    export = client.get("/api/export").json()
    row = export["stock_transactions"][0]
    assert (row["transfer_group_id"], row["external_account"]) == ("group-1", "bank:3")

    client.post("/api/import", json=export)
    restored = client.get("/api/transactions").json()[0]
    assert restored["transfer_group_id"] == "group-1"
    assert restored["external_account"] == "bank:3"
