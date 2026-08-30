"""Account editing, the account's own payee for bank charges, regex aliases,
and the transfer-rule matching window - all through the real pages."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def ledger_api(stack):
    return TestClient(stack["ledger"])


def make_account(logged_in, name="Checking", kind="checking", opening="0"):
    return logged_in.post(
        "/accounts",
        data={
            "name": name,
            "type": kind,
            "currency_code": "USD",
            "opening_balance": opening,
            "note": "",
        },
    )


class TestAccountEditing:
    def test_the_list_offers_an_edit_link_and_a_form(self, logged_in):
        make_account(logged_in)
        page = logged_in.get("/accounts")
        assert "/accounts?edit=1" in page.text

        form = logged_in.get("/accounts?edit=1")
        assert "Edit account: Checking" in form.text
        assert 'name="opening_balance"' in form.text
        assert 'name="currency_code"' in form.text
        assert 'name="type"' in form.text

    def test_editing_saves_every_field(self, logged_in, ledger_api):
        make_account(logged_in)
        response = logged_in.post(
            "/accounts/1/update",
            data={
                "name": "Sapphire Card",
                "type": "credit_card",
                "currency_code": "CAD",
                "opening_balance": "-1250.00",
                "note": "travel card",
                "active": "on",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        account = ledger_api.get("/api/accounts/1").json()
        assert account["name"] == "Sapphire Card"
        assert account["type"] == "credit_card"
        assert account["currency_code"] == "CAD"
        assert account["opening_balance"] == "-1250.00"

    def test_correcting_the_opening_balance_shifts_the_register(
        self, logged_in, ledger_api
    ):
        """The credit-card case: the export has no starting balance, so import
        first, compare with the real card, then correct here."""
        make_account(logged_in, name="Card", kind="credit_card", opening="0")
        ledger_api.post(
            "/api/transactions",
            json={
                "account_id": 1,
                "date": "2026-08-01",
                "splits": [{"category_id": None, "amount": "-60.00"}],
            },
        )
        assert ledger_api.get("/api/accounts/1").json()["balance"] == "-60.00"
        logged_in.post(
            "/accounts/1/update",
            data={"name": "Card", "type": "credit_card", "currency_code": "USD",
                  "opening_balance": "-400.00", "note": "", "active": "on"},
        )
        assert ledger_api.get("/api/accounts/1").json()["balance"] == "-460.00"

    def test_deactivate_button_does_not_wipe_the_other_fields(
        self, logged_in, ledger_api
    ):
        """The list's button only posts name and note; everything it doesn't
        offer must be left alone rather than reset."""
        make_account(logged_in, opening="750.00")
        logged_in.post("/accounts/1/update", data={"name": "Checking", "note": ""})
        account = ledger_api.get("/api/accounts/1").json()
        assert account["active"] is False
        assert account["opening_balance"] == "750.00"
        assert account["type"] == "checking"

    def test_a_rejected_edit_returns_to_the_form(self, logged_in, ledger_api):
        make_account(logged_in)
        ledger_api.post(
            "/api/transactions",
            json={
                "account_id": 1,
                "date": "2026-08-01",
                "splits": [{"category_id": None, "amount": "-1.00"}],
            },
        )
        response = logged_in.post(
            "/accounts/1/update",
            data={"name": "Checking", "type": "checking", "currency_code": "CAD",
                  "opening_balance": "0", "note": "", "active": "on"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "edit=1" in response.headers["location"]
        assert "err=" in response.headers["location"]


class TestAccountPayee:
    def test_creating_an_account_creates_its_payee(self, logged_in, ledger_api):
        make_account(logged_in, name="Sapphire Card", kind="credit_card")
        names = {p["name"] for p in ledger_api.get("/api/payees").json()}
        assert "Sapphire Card" in names

    def test_it_shows_up_on_the_payees_page(self, logged_in):
        make_account(logged_in, name="Sapphire Card", kind="credit_card")
        assert "Sapphire Card" in logged_in.get("/payees").text


class TestRegexAliasUI:
    def test_the_alias_form_offers_regex(self, logged_in, ledger_api):
        payee = ledger_api.post("/api/payees", json={"name": "Expedia"}).json()
        page = logged_in.get(f"/payees?show={payee['id']}")
        assert 'value="regex"' in page.text

    def test_a_regex_alias_saved_from_the_page_maps_every_variant(
        self, logged_in, ledger_api
    ):
        make_account(logged_in)
        payee = ledger_api.post("/api/payees", json={"name": "Expedia"}).json()
        logged_in.post(
            f"/payees/{payee['id']}/aliases",
            data={"pattern": r"^EXPEDIA INC\. \d+ - DIR DEP$", "match_type": "regex"},
        )
        ledger_api.post(
            "/api/import/profiles",
            json={
                "name": "Bank",
                "account_id": 1,
                "config": {
                    "date_column": "date",
                    "date_format": "%m/%d/%Y",
                    "description_column": "description",
                    "amount_column": "amount",
                },
            },
        )
        content = (
            "Date,Description,Amount\n"
            "08/01/2026,EXPEDIA INC. 00000000000000004085 - DIR DEP,-120.00\n"
            "08/09/2026,EXPEDIA INC. 00000000000000009912 - DIR DEP,-340.00\n"
        )
        logged_in.post(
            "/import/preview",
            data={"profile_id": "bank:1", "account_id": ""},
            files={"file": ("aug.csv", content, "text/csv")},
        )
        batch = ledger_api.get("/api/import/batches/1").json()
        assert batch["row_counts"] == {"ready": 2}
        assert {r["payee_name"] for r in batch["rows"]} == {"Expedia"}

    def test_a_broken_regex_is_reported_not_swallowed(self, logged_in, ledger_api):
        payee = ledger_api.post("/api/payees", json={"name": "Expedia"}).json()
        response = logged_in.post(
            f"/payees/{payee['id']}/aliases",
            data={"pattern": "EXPEDIA (unclosed", "match_type": "regex"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "err=" in response.headers["location"]


class TestTransferRuleWindowUI:
    def test_the_form_offers_a_window_and_saves_it(self, logged_in, ledger_api):
        make_account(logged_in, name="Checking")
        make_account(logged_in, name="Savings", kind="savings")
        page = logged_in.get("/settings")
        assert 'name="match_days"' in page.text

        logged_in.post(
            "/settings/transfer-rules",
            data={"pattern": "TO SAVINGS", "match_type": "contains",
                  "account_id": "2", "match_days": "9"},
        )
        assert ledger_api.get("/api/transfer-rules").json()[0]["match_days"] == 9
        assert "9d" in logged_in.get("/settings").text
