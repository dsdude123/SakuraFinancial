"""The factory reset. The gates are the feature: a wipe must be unreachable
until a backup exists AND has actually been handed to the browser."""

from io import BytesIO
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def stocks_api(stack):
    return TestClient(stack["stocks"])


@pytest.fixture()
def settings_api(stack):
    return TestClient(stack["settings"])


@pytest.fixture()
def populated(logged_in, ledger_api, stocks_api):
    """A stack with something to lose in every service."""
    ledger_api.post("/api/accounts", json={"name": "Checking", "type": "checking"})
    category = ledger_api.post("/api/categories", json={"name": "Food"}).json()
    payee = ledger_api.post("/api/payees", json={"name": "Safeway"}).json()
    ledger_api.post(
        "/api/transactions",
        json={
            "account_id": 1,
            "date": "2026-08-01",
            "payee_id": payee["id"],
            "splits": [{"category_id": category["id"], "amount": "-42.00"}],
        },
    )
    stocks_api.post("/api/accounts", json={"name": "Brokerage", "type": "brokerage"})
    logged_in.post("/settings/llm", data={"provider": "openai", "api_key": "sk-secret",
                                          "model": "gpt-4", "base_url": ""})
    return {"ledger": ledger_api, "stocks": stocks_api}


def start_backup(logged_in) -> str:
    """Run step 2 and return the token the redirect carries."""
    response = logged_in.post("/reset/backup", follow_redirects=False)
    assert response.status_code == 303
    return response.headers["location"].split("token=")[1]


class TestEntryPoint:
    def test_settings_page_links_to_the_reset(self, logged_in):
        page = logged_in.get("/settings")
        assert "Reset everything" in page.text
        assert 'href="/reset"' in page.text

    def test_first_step_only_offers_the_backup(self, logged_in):
        page = logged_in.get("/reset")
        assert "Step 1 of 3" in page.text
        assert "Create the safety backup" in page.text
        # No way to reach the wipe from here.
        assert "Erase everything now" not in page.text


class TestBackupGate:
    def test_backup_is_built_and_offered(self, logged_in, populated):
        token = start_backup(logged_in)
        page = logged_in.get(f"/reset?token={token}")
        assert "Step 2 of 3" in page.text
        assert "sakura-backup-" in page.text
        assert "not downloaded yet" in page.text

    def test_downloaded_zip_contains_every_service(self, logged_in, populated):
        token = start_backup(logged_in)
        response = logged_in.get(f"/reset/download?token={token}")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        names = set(ZipFile(BytesIO(response.content)).namelist())
        assert {"ledger.json", "stocks.json", "settings.json", "budget.json",
                "receipts.json", "receipt-files.zip"} <= names

    def test_page_reflects_the_download(self, logged_in, populated):
        token = start_backup(logged_in)
        logged_in.get(f"/reset/download?token={token}")
        page = logged_in.get(f"/reset?token={token}")
        assert "downloaded" in page.text
        assert "not downloaded yet" not in page.text

    def test_wipe_is_refused_until_the_backup_is_downloaded(
        self, logged_in, populated, ledger_api
    ):
        token = start_backup(logged_in)
        response = logged_in.post(
            "/reset/confirm",
            data={"token": token, "confirm": "ERASE", "understood": "on"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "Download%20the%20backup%20first" in response.headers["location"]
        # Nothing touched.
        assert len(ledger_api.get("/api/transactions").json()) == 1

    def test_unknown_token_cannot_wipe(self, logged_in, populated, ledger_api):
        logged_in.post(
            "/reset/confirm",
            data={"token": "made-up", "confirm": "ERASE", "understood": "on"},
        )
        assert len(ledger_api.get("/api/transactions").json()) == 1


class TestConfirmationGate:
    def ready(self, logged_in):
        token = start_backup(logged_in)
        logged_in.get(f"/reset/download?token={token}")
        return token

    def test_unticked_box_refuses(self, logged_in, populated, ledger_api):
        token = self.ready(logged_in)
        logged_in.post("/reset/confirm", data={"token": token, "confirm": "ERASE"})
        assert len(ledger_api.get("/api/transactions").json()) == 1

    def test_wrong_word_refuses(self, logged_in, populated, ledger_api):
        token = self.ready(logged_in)
        logged_in.post(
            "/reset/confirm",
            data={"token": token, "confirm": "yes", "understood": "on"},
        )
        assert len(ledger_api.get("/api/transactions").json()) == 1

    def test_the_word_is_accepted_in_any_case(self, logged_in, populated, ledger_api):
        token = self.ready(logged_in)
        logged_in.post(
            "/reset/confirm",
            data={"token": token, "confirm": " erase ", "understood": "on"},
        )
        assert ledger_api.get("/api/transactions").json() == []


class TestTheWipe:
    def confirm(self, logged_in):
        token = start_backup(logged_in)
        logged_in.get(f"/reset/download?token={token}")
        return logged_in.post(
            "/reset/confirm",
            data={"token": token, "confirm": "ERASE", "understood": "on"},
            follow_redirects=False,
        )

    def test_every_service_is_emptied(self, logged_in, populated, ledger_api, stocks_api):
        response = self.confirm(logged_in)
        assert response.status_code == 303
        assert ledger_api.get("/api/transactions").json() == []
        assert ledger_api.get("/api/accounts").json() == []
        assert ledger_api.get("/api/categories").json() == []
        assert ledger_api.get("/api/payees").json() == []
        assert stocks_api.get("/api/accounts").json() == []

    def test_currencies_survive_so_the_app_is_usable(self, logged_in, populated, ledger_api):
        self.confirm(logged_in)
        codes = {c["code"] for c in ledger_api.get("/api/currencies").json()}
        assert "USD" in codes
        # And a fresh account can actually be created afterwards.
        created = ledger_api.post("/api/accounts", json={"name": "New", "type": "checking"})
        assert created.status_code in (200, 201)

    def test_saved_api_keys_are_cleared(self, logged_in, populated, settings_api):
        self.confirm(logged_in)
        keys = {row["key"] for row in settings_api.get("/api/settings").json()}
        assert "llm.api_key" not in keys
        assert "llm.provider" not in keys

    def test_the_login_password_survives_so_you_are_not_locked_out(
        self, logged_in, populated, settings_api
    ):
        self.confirm(logged_in)
        keys = {row["key"] for row in settings_api.get("/api/settings").json()}
        assert "ui.password_hash" in keys
        # Still signed in, and the app still works.
        assert logged_in.get("/settings").status_code == 200

    def test_the_parked_backup_is_cleaned_up(self, logged_in, populated):
        token = start_backup(logged_in)
        logged_in.get(f"/reset/download?token={token}")
        logged_in.post(
            "/reset/confirm", data={"token": token, "confirm": "ERASE", "understood": "on"}
        )
        # The token is spent: it can neither be downloaded nor replayed.
        assert logged_in.get(f"/reset/download?token={token}", follow_redirects=False).status_code == 303

    def test_a_reset_stack_can_be_restored_from_its_own_backup(
        self, logged_in, populated, ledger_api
    ):
        token = start_backup(logged_in)
        zip_bytes = logged_in.get(f"/reset/download?token={token}").content
        logged_in.post(
            "/reset/confirm", data={"token": token, "confirm": "ERASE", "understood": "on"}
        )
        assert ledger_api.get("/api/transactions").json() == []

        restored = logged_in.post(
            "/backup/restore",
            data={"confirm": "on"},
            files={"file": ("backup.zip", zip_bytes, "application/zip")},
            follow_redirects=True,
        )
        assert restored.status_code == 200
        assert len(ledger_api.get("/api/transactions").json()) == 1
        assert {a["name"] for a in ledger_api.get("/api/accounts").json()} == {"Checking"}


class TestCancel:
    def test_cancelling_discards_the_backup_and_changes_nothing(
        self, logged_in, populated, ledger_api
    ):
        token = start_backup(logged_in)
        response = logged_in.post(
            "/reset/cancel", data={"token": token}, follow_redirects=False
        )
        assert response.status_code == 303
        assert "/settings" in response.headers["location"]
        assert len(ledger_api.get("/api/transactions").json()) == 1
        # The parked copy is gone, so the token can no longer reach a wipe.
        logged_in.post(
            "/reset/confirm", data={"token": token, "confirm": "ERASE", "understood": "on"}
        )
        assert len(ledger_api.get("/api/transactions").json()) == 1
