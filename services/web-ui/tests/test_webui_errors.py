"""Error presentation: readable labels, readable messages, and a stack trace
you can reach from the page instead of from docker logs."""

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from webui_service.rendering import friendly, humanize


class TestHumanize:
    def test_known_values_get_a_written_label(self):
        assert humanize("needs_payee") == "Needs a payee"
        assert humanize("credit_card") == "Credit card"
        assert humanize("no_lots") == "No shares held"
        assert humanize("counterpart") == "Other side of a transfer"
        assert humanize("duplicate") == "Already imported"

    def test_unknown_values_are_still_tidied(self):
        assert humanize("some_new_status") == "Some new status"

    def test_blanks_stay_blank(self):
        assert humanize(None) == ""
        assert humanize("") == ""


class TestFriendly:
    def test_a_service_message_becomes_a_sentence(self):
        assert friendly("no account 3") == "No account 3."

    def test_existing_punctuation_is_kept(self):
        assert friendly("Account 3 no longer exists.") == "Account 3 no longer exists."
        assert friendly("Really?") == "Really?"

    def test_blank_stays_blank(self):
        assert friendly(None) == ""
        assert friendly("   ") == ""


class TestRawValuesNeverReachThePage:
    def test_account_types_are_written_out(self, logged_in):
        logged_in.post(
            "/accounts",
            data={"name": "Card", "type": "credit_card", "currency_code": "USD",
                  "opening_balance": "0", "note": ""},
        )
        page = logged_in.get("/accounts")
        assert "Credit card" in page.text
        assert ">credit_card<" not in page.text

    def test_import_statuses_are_written_out(self, logged_in, ledger_api):
        logged_in.post(
            "/accounts",
            data={"name": "Checking", "type": "checking", "currency_code": "USD",
                  "opening_balance": "0", "note": ""},
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
        logged_in.post(
            "/import/preview",
            data={"profile_id": "bank:1", "account_id": ""},
            files={"file": ("aug.csv", "Date,Description,Amount\n08/01/2026,SHOP,-5.00\n", "text/csv")},
        )
        page = logged_in.get("/import/batches/bank/1")
        assert "Needs a payee" in page.text
        assert "needs_payee" not in page.text


class TestErrorPageAndTrace:
    def test_a_rejected_request_explains_itself_without_a_trace(self, logged_in, ledger_api):
        """A deliberate rejection is not a crash, so there is nothing to trace."""
        ledger_api.post(
            "/api/import/profiles",
            json={
                "name": "No account",
                "config": {
                    "date_column": "date",
                    "date_format": "%m/%d/%Y",
                    "description_column": "description",
                    "amount_column": "amount",
                },
            },
        )
        page = logged_in.post(
            "/import/preview",
            data={"profile_id": "bank:1", "account_id": ""},
            files={"file": ("aug.csv", "Date,Description,Amount\n08/01/2026,SHOP,-5.00\n", "text/csv")},
            follow_redirects=True,
        )
        # The message reads as a sentence, and names no API field.
        banner = page.text.split('<div class="err">')[1].split("</div>")[0]
        assert "This profile has no default account" in banner
        assert "account_id" not in banner
        assert "pass account_id" not in page.text

    def test_a_crash_is_captured_and_linked(self, logged_in, stack):
        """A genuine 500 in a backend lands on the error page with a link to
        the stack trace, rather than only in the container logs."""

        @stack["ledger"].get("/api/boom")
        def boom():
            raise RuntimeError("the thing exploded")

        @stack["webui"].get("/boom")
        async def proxy(request: Request):
            return await request.app.state.clients.ledger.get("/api/boom")

        page = logged_in.get("/boom")
        assert page.status_code == 200
        assert "That didn't work" in page.text
        assert "Show the technical details" in page.text

        detail_url = page.text.split('action="/errors/')[1].split('"')[0]
        detail = logged_in.get(f"/errors/{detail_url}")
        assert "RuntimeError: the thing exploded" in detail.text
        assert "Stack trace" in detail.text
        assert "/api/boom" in detail.text

    def test_the_error_log_lists_recent_failures(self, logged_in, stack):
        @stack["ledger"].get("/api/boom2")
        def boom():
            raise RuntimeError("second explosion")

        @stack["webui"].get("/boom2")
        async def proxy(request: Request):
            return await request.app.state.clients.ledger.get("/api/boom2")

        logged_in.get("/boom2")
        log = logged_in.get("/errors")
        assert "second explosion" not in log.text  # the list shows the message, not the trace
        assert "ledger" in log.text
        assert "Details" in log.text

    def test_an_empty_log_says_so(self, logged_in):
        assert "Nothing has gone wrong" in logged_in.get("/errors").text

    def test_an_unknown_error_id_is_explained(self, logged_in):
        page = logged_in.get("/errors/deadbeef")
        assert "no longer in the log" in page.text
