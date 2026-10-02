"""Importing a bank statement that moves money to a brokerage.

A transfer rule can name an investment account, which lives in the stocks
service. The import then books this side — one categoryless leg — and reports
what is left to settle over there, because the ledger never calls another
service to move money.
"""

import pytest

PROFILE_CONFIG = {
    "date_column": "date",
    "date_format": "%m/%d/%Y",
    "description_column": "description",
    "amount_column": "amount",
}

WIRE_OUT = "Date,Description,Amount\n09/10/2026,FIDELITY WIRE OUT,-2500.00\n"


@pytest.fixture()
def profile(client, checking):
    return client.post(
        "/api/import/profiles",
        json={"name": "Test Bank", "account_id": checking["id"], "config": PROFILE_CONFIG},
    ).json()


@pytest.fixture()
def brokerage_rule(client):
    return client.post(
        "/api/transfer-rules",
        json={
            "pattern": "FIDELITY",
            "match_type": "prefix",
            "external_account": "stock:1",
        },
    ).json()


def preview(client, profile, content=WIRE_OUT):
    return client.post(
        "/api/import/preview",
        json={"profile_id": profile["id"], "filename": "bank.csv", "content": content},
    )


class TestRules:
    def test_a_rule_can_name_an_investment_account(self, client):
        rule = client.post(
            "/api/transfer-rules",
            json={"pattern": "FIDELITY", "external_account": "stock:1"},
        )
        assert rule.status_code == 200
        assert rule.json()["external_account"] == "stock:1"
        assert rule.json()["account_id"] is None

    def test_a_bank_rule_still_works(self, client, savings):
        rule = client.post(
            "/api/transfer-rules", json={"pattern": "VENMO", "account_id": savings["id"]}
        )
        assert rule.status_code == 200
        assert rule.json()["external_account"] is None
        assert rule.json()["account_name"] == "Savings"

    def test_not_both(self, client, savings):
        response = client.post(
            "/api/transfer-rules",
            json={
                "pattern": "FIDELITY",
                "account_id": savings["id"],
                "external_account": "stock:1",
            },
        )
        assert response.status_code == 422
        assert "not both" in response.json()["detail"]

    def test_not_neither(self, client):
        response = client.post("/api/transfer-rules", json={"pattern": "FIDELITY"})
        assert response.status_code == 422
        assert "needs a counter account" in response.json()["detail"]

    @pytest.mark.parametrize("ref", ["stock:", "stock:abc", "brokerage:1", "1", "stock:1:2"])
    def test_a_ref_that_names_nothing(self, client, ref):
        response = client.post(
            "/api/transfer-rules", json={"pattern": "FIDELITY", "external_account": ref}
        )
        assert response.status_code == 422

    def test_an_external_rule_can_be_edited(self, client, brokerage_rule, savings):
        """Point the same rule at a bank account instead: the external ref goes."""
        updated = client.put(
            f"/api/transfer-rules/{brokerage_rule['id']}",
            json={"pattern": "FIDELITY", "account_id": savings["id"]},
        ).json()
        assert updated["external_account"] is None
        assert updated["account_id"] == savings["id"]

    def test_a_backup_round_trip_keeps_the_target(self, client, brokerage_rule):
        export = client.get("/api/export").json()
        assert export["transfer_rules"][0]["external_account"] == "stock:1"
        client.post("/api/import", json=export)
        assert client.get("/api/transfer-rules").json()[0]["external_account"] == "stock:1"


class TestClassification:
    def test_a_matching_row_becomes_a_transfer_to_the_investment_account(
        self, client, profile, brokerage_rule
    ):
        row = preview(client, profile).json()["rows"][0]
        assert row["status"] == "transfer"
        assert row["transfer_external_account"] == "stock:1"
        assert row["transfer_account_id"] is None

    def test_reclassify_picks_up_a_rule_added_mid_review(self, client, profile):
        batch = preview(client, profile).json()
        assert batch["rows"][0]["status"] == "needs_payee"
        client.post(
            "/api/transfer-rules",
            json={"pattern": "FIDELITY", "external_account": "stock:2"},
        )
        result = client.post(f"/api/import/batches/{batch['id']}/reclassify").json()
        assert result["changed"] == 1
        row = client.get(f"/api/import/batches/{batch['id']}").json()["rows"][0]
        assert row["status"] == "transfer"
        assert row["transfer_external_account"] == "stock:2"

    def test_the_reviewer_can_choose_an_investment_account_by_hand(self, client, profile):
        batch = preview(client, profile).json()
        row = client.put(
            f"/api/import/rows/{batch['rows'][0]['id']}",
            json={"transfer_external_account": "stock:3"},
        ).json()
        assert row["status"] == "transfer"
        assert row["transfer_external_account"] == "stock:3"

    def test_choosing_a_bank_account_clears_the_investment_one(
        self, client, profile, brokerage_rule, savings
    ):
        batch = preview(client, profile).json()
        row = client.put(
            f"/api/import/rows/{batch['rows'][0]['id']}",
            json={"transfer_account_id": savings["id"]},
        ).json()
        assert row["transfer_external_account"] is None
        assert row["transfer_account_id"] == savings["id"]

    def test_a_row_already_recorded_by_hand_is_flagged_counterpart(
        self, client, profile, checking, brokerage_rule
    ):
        """The transfer was entered in the register before the statement
        arrived. Importing the row as well would move the money twice, and the
        row hash can't tell — a hand-entered transfer never had one."""
        client.post(
            "/api/transfers/external",
            json={
                "account_id": checking["id"],
                "date": "2026-09-08",  # two days off, inside the rule's window
                "amount": "2500.00",
                "direction": "out",
                "external_account": "stock:1",
            },
        )
        row = preview(client, profile).json()["rows"][0]
        assert row["status"] == "counterpart"
        assert row["include"] is False

    def test_a_different_amount_is_not_the_same_transfer(
        self, client, profile, checking, brokerage_rule
    ):
        client.post(
            "/api/transfers/external",
            json={
                "account_id": checking["id"],
                "date": "2026-09-10",
                "amount": "100.00",
                "direction": "out",
                "external_account": "stock:1",
            },
        )
        assert preview(client, profile).json()["rows"][0]["status"] == "transfer"

    def test_a_transfer_to_a_different_brokerage_is_not_the_same_transfer(
        self, client, profile, checking, brokerage_rule
    ):
        client.post(
            "/api/transfers/external",
            json={
                "account_id": checking["id"],
                "date": "2026-09-10",
                "amount": "2500.00",
                "direction": "out",
                "external_account": "stock:9",
            },
        )
        assert preview(client, profile).json()["rows"][0]["status"] == "transfer"


class TestCommit:
    def test_one_leg_here_and_a_settlement_to_make_there(
        self, client, profile, checking, brokerage_rule
    ):
        batch = preview(client, profile).json()
        summary = client.post(f"/api/import/batches/{batch['id']}/commit").json()
        assert summary["transfers"] == 1
        assert summary["created"] == 0
        assert summary["account_id"] == checking["id"]

        pending = summary["external_transfers"]
        assert len(pending) == 1
        assert pending[0]["external_account"] == "stock:1"
        assert pending[0]["direction"] == "out"
        assert pending[0]["amount"] == "2500.00"
        assert pending[0]["date"] == "2026-09-10"

        legs = client.get("/api/transactions", params={"kind": "transfer"}).json()
        assert len(legs) == 1
        assert legs[0]["external_account"] == "stock:1"
        assert legs[0]["total"] == "-2500.00"
        assert legs[0]["transfer_group_id"] == pending[0]["transfer_group_id"]
        assert legs[0]["splits"][0]["category_id"] is None
        assert client.get(f"/api/accounts/{checking['id']}").json()["balance"] == "-1500.00"

    def test_money_coming_back_from_the_brokerage(self, client, profile, brokerage_rule):
        batch = preview(
            client, profile, "Date,Description,Amount\n09/10/2026,FIDELITY WIRE IN,400.00\n"
        ).json()
        summary = client.post(f"/api/import/batches/{batch['id']}/commit").json()
        assert summary["external_transfers"][0]["direction"] == "in"
        assert client.get("/api/transactions").json()[0]["total"] == "400.00"

    def test_it_never_lands_in_spending(self, client, profile, brokerage_rule):
        batch = preview(client, profile).json()
        client.post(f"/api/import/batches/{batch['id']}/commit")
        tree = client.get(
            "/api/reports/category-tree", params={"start": "2026-09-01", "end": "2026-09-30"}
        ).json()
        assert tree == []

    def test_re_importing_the_statement_sees_a_duplicate(self, client, profile, brokerage_rule):
        first = preview(client, profile).json()
        client.post(f"/api/import/batches/{first['id']}/commit")
        again = preview(client, profile).json()
        assert again["rows"][0]["status"] == "duplicate"

    def test_an_import_with_no_investment_transfers_reports_none(self, client, profile):
        batch = preview(
            client, profile, "Date,Description,Amount\n09/10/2026,QFC,-20.00\n"
        ).json()
        summary = client.post(f"/api/import/batches/{batch['id']}/commit").json()
        assert summary["external_transfers"] == []

    def test_undoing_the_leg_releases_the_row_it_came_from(
        self, client, profile, checking, brokerage_rule
    ):
        """What the caller does when the other service refuses its half: delete
        the leg. The import row must let go of it, or re-importing the statement
        would call that row a duplicate of a transaction that no longer exists —
        and on Postgres the delete would fail outright on the foreign key."""
        batch = preview(client, profile).json()
        summary = client.post(f"/api/import/batches/{batch['id']}/commit").json()
        leg_id = summary["external_transfers"][0]["transaction_id"]

        assert client.delete(f"/api/transactions/{leg_id}").status_code == 200
        row = client.get(f"/api/import/batches/{batch['id']}").json()["rows"][0]
        assert row["transaction_id"] is None
        assert client.get(f"/api/accounts/{checking['id']}").json()["balance"] == "1000.00"

        again = preview(client, profile).json()
        assert again["rows"][0]["status"] == "transfer"  # offered afresh, not a duplicate
