"""Importing a bank statement whose transfers go to a brokerage, end to end.

One row, two services: the ledger books its leg when the batch commits and the
web UI settles the cash in the stocks service straight after. These tests drive
the real pages — settings to add the rule, upload, review, commit — and then
check both books.
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def ledger_api(stack):
    return TestClient(stack["ledger"])


@pytest.fixture()
def stocks_api(stack):
    return TestClient(stack["stocks"])


@pytest.fixture()
def checking(ledger_api):
    return ledger_api.post(
        "/api/accounts",
        json={"name": "Checking", "type": "checking", "opening_balance": "5000.00"},
    ).json()


@pytest.fixture()
def brokerage(stocks_api):
    return stocks_api.post(
        "/api/accounts",
        json={"name": "Fidelity", "type": "brokerage", "opening_cash": "1000.00"},
    ).json()


@pytest.fixture()
def profile(logged_in, checking):
    logged_in.post(
        "/import/profiles",
        data={
            "name": "Test Bank",
            "kind": "bank",
            "account_id": f"bank:{checking['id']}",
            "delimiter": ",",
            "has_header": "on",
            "skip_top_rows": "0",
            "date_column": "date",
            "date_format": "%m/%d/%Y",
            "description_column": "description",
            "amount_mode": "single",
            "amount_column": "amount",
        },
    )
    return 1


WIRE_CSV = "Date,Description,Amount\n09/10/2026,FIDELITY WIRE OUT,-2500.00\n"


def add_rule(logged_in, target: str, pattern: str = "FIDELITY"):
    return logged_in.post(
        "/settings/transfer-rules",
        data={"pattern": pattern, "match_type": "prefix", "account_id": target, "match_days": "5"},
        follow_redirects=True,
    )


def upload(logged_in, profile, content=WIRE_CSV):
    return logged_in.post(
        "/import/preview",
        data={"profile_id": f"bank:{profile}", "account_id": ""},
        files={"file": ("sep.csv", content, "text/csv")},
        follow_redirects=True,
    )


class TestTheRuleItself:
    def test_the_picker_offers_investment_accounts(self, logged_in, checking, brokerage):
        page = logged_in.get("/settings")
        assert 'value="stock:1"' in page.text
        assert 'value="bank:1"' in page.text

    def test_an_investment_rule_lists_with_the_account_name(
        self, logged_in, checking, brokerage
    ):
        add_rule(logged_in, f"stock:{brokerage['id']}")
        page = logged_in.get("/settings")
        assert "Fidelity" in page.text
        assert "investment" in page.text

    def test_a_bank_rule_still_lists(self, logged_in, ledger_api, checking):
        savings = ledger_api.post(
            "/api/accounts", json={"name": "Savings", "type": "savings"}
        ).json()
        add_rule(logged_in, f"bank:{savings['id']}", pattern="VENMO")
        page = logged_in.get("/settings")
        assert "Savings" in page.text


class TestImporting:
    def test_a_wire_to_the_brokerage_imports_as_a_transfer(
        self, logged_in, ledger_api, stocks_api, checking, brokerage, profile
    ):
        add_rule(logged_in, f"stock:{brokerage['id']}")
        review = upload(logged_in, profile)
        assert "Transfer" in review.text
        assert "Fidelity" in review.text

        summary = logged_in.post(
            "/import/batches/bank/1/commit", data={"name_from_description": "on"}
        )
        assert "<b>1</b> transfer(s)" in summary.text
        assert "both sides are recorded" in summary.text

        assert ledger_api.get(f"/api/accounts/{checking['id']}").json()["balance"] == "2500.00"
        assert stocks_api.get(f"/api/accounts/{brokerage['id']}").json()["cash"] == "3500.00"

        leg = ledger_api.get("/api/transactions").json()[0]
        far = stocks_api.get("/api/transactions").json()[0]
        assert leg["kind"] == "transfer"
        assert leg["external_account"] == f"stock:{brokerage['id']}"
        assert far["type"] == "deposit"
        assert far["external_account"] == f"bank:{checking['id']}"
        assert far["transfer_group_id"] == leg["transfer_group_id"]

    def test_money_coming_back_becomes_a_withdrawal(
        self, logged_in, ledger_api, stocks_api, checking, brokerage, profile
    ):
        add_rule(logged_in, f"stock:{brokerage['id']}")
        upload(logged_in, profile, "Date,Description,Amount\n09/10/2026,FIDELITY WIRE IN,400.00\n")
        logged_in.post("/import/batches/bank/1/commit", data={"name_from_description": "on"})
        assert ledger_api.get(f"/api/accounts/{checking['id']}").json()["balance"] == "5400.00"
        assert stocks_api.get(f"/api/accounts/{brokerage['id']}").json()["cash"] == "600.00"
        assert stocks_api.get("/api/transactions").json()[0]["type"] == "withdraw"

    def test_it_is_not_spending(self, logged_in, ledger_api, checking, brokerage, profile):
        add_rule(logged_in, f"stock:{brokerage['id']}")
        upload(logged_in, profile)
        logged_in.post("/import/batches/bank/1/commit", data={"name_from_description": "on"})
        tree = ledger_api.get(
            "/api/reports/category-tree",
            params={"start": "2026-09-01", "end": "2026-09-30"},
        ).json()
        assert tree == []
        page = logged_in.get("/reports/spending?month=2026-09")
        assert "2,500.00" not in page.text

    def test_the_reviewer_can_send_a_row_to_a_brokerage_by_hand(
        self, logged_in, ledger_api, stocks_api, checking, brokerage, profile
    ):
        """No rule at all: the row arrives as needs_payee, and the review screen
        can still point it at the investment account."""
        upload(logged_in, profile)
        row = ledger_api.get("/api/import/batches/1").json()["rows"][0]
        assert row["status"] == "needs_payee"
        logged_in.post(
            f"/import/rows/{row['id']}",
            data={
                "batch_id": "1",
                "transfer_account_id": f"stock:{brokerage['id']}",
                "include": "on",
            },
        )
        updated = ledger_api.get("/api/import/batches/1").json()["rows"][0]
        assert updated["status"] == "transfer"
        assert updated["transfer_external_account"] == f"stock:{brokerage['id']}"

        logged_in.post("/import/batches/bank/1/commit", data={"name_from_description": "on"})
        assert stocks_api.get(f"/api/accounts/{brokerage['id']}").json()["cash"] == "3500.00"

    def test_bank_to_bank_rows_are_unaffected(
        self, logged_in, ledger_api, checking, profile
    ):
        savings = ledger_api.post(
            "/api/accounts", json={"name": "Savings", "type": "savings"}
        ).json()
        add_rule(logged_in, f"bank:{savings['id']}", pattern="TRANSFER TO SAVINGS")
        upload(
            logged_in,
            profile,
            "Date,Description,Amount\n09/10/2026,TRANSFER TO SAVINGS,-100.00\n",
        )
        summary = logged_in.post(
            "/import/batches/bank/1/commit", data={"name_from_description": "on"}
        )
        assert "<b>1</b> transfer(s)" in summary.text
        assert "both sides are recorded" not in summary.text  # nothing to settle elsewhere
        assert ledger_api.get(f"/api/accounts/{savings['id']}").json()["balance"] == "100.00"

    def test_the_second_statement_of_the_same_wire_is_not_imported_twice(
        self, logged_in, stocks_api, checking, brokerage, profile
    ):
        """The bank import books both sides; the broker's own activity file then
        reports the same wire, and must not credit it again."""
        add_rule(logged_in, f"stock:{brokerage['id']}")
        upload(logged_in, profile)
        logged_in.post("/import/batches/bank/1/commit", data={"name_from_description": "on"})

        stocks_api.post(
            "/api/import/profiles",
            json={
                "name": "Broker",
                "account_id": brokerage["id"],
                "config": {
                    "date_column": "date",
                    "date_format": "%m/%d/%Y",
                    "action_column": "type",
                    "amount_column": "amount",
                    "action_map": {"WIRE IN": "deposit"},
                },
            },
        )
        review = logged_in.post(
            "/import/preview",
            data={"profile_id": "stock:1", "account_id": ""},
            files={
                "file": (
                    "activity.csv",
                    "Date,Type,Amount\n09/12/2026,WIRE IN,2500.00\n",
                    "text/csv",
                )
            },
            follow_redirects=True,
        )
        assert "Other side of a transfer" in review.text
        logged_in.post("/import/batches/stock/1/commit")
        assert stocks_api.get(f"/api/accounts/{brokerage['id']}").json()["cash"] == "3500.00"


class TestWhenTheBrokerageSideFails:
    def test_the_leg_is_undone_and_the_row_is_offered_again(
        self, logged_in, stack, ledger_api, stocks_api, checking, brokerage, profile
    ):
        add_rule(logged_in, f"stock:{brokerage['id']}")
        upload(logged_in, profile)

        from webui_service.clients import ServiceError

        real_post = stack["webui"].state.clients.stocks.post

        async def failing_post(path, json=None):
            if path == "/api/transfers/external":
                raise ServiceError("stocks", 500, "stocks database is on fire")
            return await real_post(path, json=json)

        stack["webui"].state.clients.stocks.post = failing_post
        summary = logged_in.post(
            "/import/batches/bank/1/commit", data={"name_from_description": "on"}
        )
        assert "could not be completed" in summary.text
        assert "on fire" in summary.text
        assert "<b>0</b> transfer(s)" in summary.text

        # Nothing left half-done: no leg, no cash, and the money still in the bank.
        assert ledger_api.get("/api/transactions").json() == []
        assert ledger_api.get(f"/api/accounts/{checking['id']}").json()["balance"] == "5000.00"
        assert stocks_api.get(f"/api/accounts/{brokerage['id']}").json()["cash"] == "1000.00"

        # And the statement can simply be imported again.
        stack["webui"].state.clients.stocks.post = real_post
        upload(logged_in, profile)
        row = ledger_api.get("/api/import/batches/2").json()["rows"][0]
        assert row["status"] == "transfer"
        logged_in.post("/import/batches/bank/2/commit", data={"name_from_description": "on"})
        assert stocks_api.get(f"/api/accounts/{brokerage['id']}").json()["cash"] == "3500.00"

    def test_an_account_that_no_longer_exists_drops_only_that_row(
        self, logged_in, ledger_api, stocks_api, checking, brokerage, profile
    ):
        add_rule(logged_in, "stock:99")
        add_rule(logged_in, f"stock:{brokerage['id']}", pattern="SCHWAB")
        upload(
            logged_in,
            profile,
            "Date,Description,Amount\n"
            "09/10/2026,FIDELITY WIRE OUT,-2500.00\n"
            "09/11/2026,SCHWAB WIRE OUT,-100.00\n",
        )
        summary = logged_in.post(
            "/import/batches/bank/1/commit", data={"name_from_description": "on"}
        )
        assert "could not be completed" in summary.text
        assert "<b>1</b> transfer(s)" in summary.text

        legs = ledger_api.get("/api/transactions").json()
        assert [leg["total"] for leg in legs] == ["-100.00"]
        # The Schwab row went through; the money is in the brokerage.
        assert stocks_api.get(f"/api/accounts/{brokerage['id']}").json()["cash"] == "1100.00"
