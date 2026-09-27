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


class TestBrokerImportsDoNotDoubleCount:
    """Both statements describe one movement. The bank import books both sides,
    so the broker's own line for the same wire has to be recognised — its row
    hash never will be, because that row was never imported here."""

    PROFILE_CONFIG = {
        "date_column": "date",
        "date_format": "%m/%d/%Y",
        "action_column": "type",
        "symbol_column": "symbol",
        "quantity_column": "qty",
        "price_column": "price",
        "amount_column": "amount",
        "action_map": {"WIRE IN": "deposit", "WIRE OUT": "withdraw", "Bought": "buy"},
    }

    def profile(self, client, account, **config):
        return client.post(
            "/api/import/profiles",
            json={
                "name": f"Broker {len(config)}",
                "account_id": account["id"],
                "config": {**self.PROFILE_CONFIG, **config},
            },
        ).json()

    def preview(self, client, profile, content):
        return client.post(
            "/api/import/preview",
            json={"profile_id": profile["id"], "filename": "activity.csv", "content": content},
        ).json()

    def wire_in(self, date="09/10/2026", amount="2500.00"):
        return f"Date,Type,Symbol,Qty,Price,Amount\n{date},WIRE IN,,,,{amount}\n"

    def test_the_far_side_of_a_booked_transfer_is_flagged(self, client, brokerage):
        transfer(client, brokerage)  # as the bank import would have booked it
        profile = self.profile(client, brokerage)
        row = self.preview(client, profile, self.wire_in())["rows"][0]
        assert row["status"] == "counterpart"
        assert row["include"] is False

    def test_committing_it_changes_nothing(self, client, brokerage):
        transfer(client, brokerage)
        profile = self.profile(client, brokerage)
        batch = self.preview(client, profile, self.wire_in())
        summary = client.post(f"/api/import/batches/{batch['id']}/commit").json()
        assert summary["created"] == 0
        assert client.get(f"/api/accounts/{brokerage['id']}").json()["cash"] == "12500.00"

    def test_a_few_days_apart_still_matches(self, client, brokerage):
        """The bank posts on the Friday, the broker on the Tuesday."""
        transfer(client, brokerage)
        profile = self.profile(client, brokerage)
        row = self.preview(client, profile, self.wire_in(date="09/14/2026"))["rows"][0]
        assert row["status"] == "counterpart"

    def test_outside_the_window_it_is_new_money(self, client, brokerage):
        transfer(client, brokerage)
        profile = self.profile(client, brokerage)
        row = self.preview(client, profile, self.wire_in(date="09/30/2026"))["rows"][0]
        assert row["status"] == "ready"

    def test_the_window_is_configurable(self, client, brokerage):
        transfer(client, brokerage)
        profile = self.profile(client, brokerage, transfer_match_days=30)
        row = self.preview(client, profile, self.wire_in(date="09/30/2026"))["rows"][0]
        assert row["status"] == "counterpart"

    def test_a_different_amount_is_new_money(self, client, brokerage):
        transfer(client, brokerage)
        profile = self.profile(client, brokerage)
        row = self.preview(client, profile, self.wire_in(amount="99.00"))["rows"][0]
        assert row["status"] == "ready"

    def test_a_deposit_nobody_linked_is_left_alone(self, client, brokerage):
        """A deposit typed in by hand says nothing about where it came from, so
        an imported row is not assumed to be it."""
        client.post(
            "/api/transactions",
            json={
                "account_id": brokerage["id"],
                "type": "deposit",
                "date": "2026-09-10",
                "amount": "2500.00",
            },
        )
        profile = self.profile(client, brokerage)
        row = self.preview(client, profile, self.wire_in())["rows"][0]
        assert row["status"] == "ready"

    def test_two_identical_lines_claim_two_different_transfers(self, client, brokerage):
        transfer(client, brokerage, transfer_group_id="group-1")
        transfer(client, brokerage, transfer_group_id="group-2")
        profile = self.profile(client, brokerage)
        batch = self.preview(
            client,
            profile,
            "Date,Type,Symbol,Qty,Price,Amount\n"
            "09/10/2026,WIRE IN,,,,2500.00\n"
            "09/11/2026,WIRE IN,,,,2500.00\n",
        )
        assert [r["status"] for r in batch["rows"]] == ["counterpart", "counterpart"]

    def test_a_third_line_beyond_the_booked_transfers_is_new_money(self, client, brokerage):
        transfer(client, brokerage)
        profile = self.profile(client, brokerage)
        batch = self.preview(
            client,
            profile,
            "Date,Type,Symbol,Qty,Price,Amount\n"
            "09/10/2026,WIRE IN,,,,2500.00\n"
            "09/11/2026,WIRE IN,,,,2500.00\n",
        )
        assert [r["status"] for r in batch["rows"]] == ["counterpart", "ready"]

    def test_withdrawals_match_the_same_way(self, client, brokerage):
        transfer(client, brokerage, direction="out", amount="300.00")
        profile = self.profile(client, brokerage)
        row = self.preview(
            client,
            profile,
            "Date,Type,Symbol,Qty,Price,Amount\n09/10/2026,WIRE OUT,,,,300.00\n",
        )["rows"][0]
        assert row["status"] == "counterpart"

    def test_a_deposit_is_not_matched_against_a_withdrawal(self, client, brokerage):
        transfer(client, brokerage, direction="out", amount="2500.00")
        profile = self.profile(client, brokerage)
        row = self.preview(client, profile, self.wire_in())["rows"][0]
        assert row["status"] == "ready"

    def test_trades_are_untouched_by_any_of_this(self, client, brokerage):
        transfer(client, brokerage)
        profile = self.profile(client, brokerage)
        batch = self.preview(
            client,
            profile,
            "Date,Type,Symbol,Qty,Price,Amount\n09/10/2026,Bought,AAPL,10,150.00,1500.00\n",
        )
        assert batch["rows"][0]["status"] == "ready"
