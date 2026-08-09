import json
from io import BytesIO
from zipfile import ZipFile

from fastapi.testclient import TestClient


def seed_everything(stack):
    """A little of everything, across services."""
    ledger = TestClient(stack["ledger"])
    ledger.post(
        "/api/accounts",
        json={"name": "Checking", "type": "checking", "opening_balance": "1000.00"},
    )
    rent = ledger.post("/api/categories", json={"name": "Rent", "kind": "expense"}).json()
    ledger.post(
        "/api/transactions",
        json={
            "account_id": 1,
            "date": "2026-08-01",
            "splits": [{"category_id": rent["id"], "amount": "-2000.00"}],
        },
    )
    budget = TestClient(stack["budget"])
    budget.put(f"/api/budget/2026-08/categories/{rent['id']}", json={"amount": "2000.00"})
    stocks = TestClient(stack["stocks"])
    stocks.post("/api/accounts", json={"name": "Fidelity", "type": "brokerage"})
    receipts = TestClient(stack["receipts"])
    receipts.post(
        "/api/documents", files={"file": ("r.txt", b"Total: $45.10", "text/plain")}
    )


class TestBackupRestore:
    def test_page_shows_service_status(self, logged_in):
        page = logged_in.get("/backup")
        assert "settings" in page.text and "ledger" in page.text

    def test_download_contains_every_service_and_files(self, logged_in, stack):
        seed_everything(stack)
        response = logged_in.get("/backup/download")
        assert response.headers["content-type"] == "application/zip"
        archive = ZipFile(BytesIO(response.content))
        names = set(archive.namelist())
        assert names == {
            "settings.json",
            "ledger.json",
            "budget.json",
            "stocks.json",
            "receipts.json",
            "receipt-files.zip",
        }
        ledger_data = json.loads(archive.read("ledger.json"))
        assert ledger_data["accounts"][0]["name"] == "Checking"
        # human-readable means amounts as strings, not floats
        assert ledger_data["transactions"][0]["splits"][0]["amount"] == "-2000.00"

    def test_restore_requires_confirmation(self, logged_in, stack):
        seed_everything(stack)
        backup = logged_in.get("/backup/download").content
        response = logged_in.post(
            "/backup/restore",
            files={"file": ("backup.zip", backup, "application/zip")},
            data={},
            follow_redirects=True,
        )
        assert "Tick the confirmation box" in response.text

    def test_full_round_trip_restore(self, logged_in, stack):
        seed_everything(stack)
        backup = logged_in.get("/backup/download").content

        # vandalize the data, then restore
        ledger = TestClient(stack["ledger"])
        txn_id = ledger.get("/api/transactions").json()[0]["id"]
        ledger.delete(f"/api/transactions/{txn_id}")
        assert ledger.get("/api/transactions").json() == []

        response = logged_in.post(
            "/backup/restore",
            files={"file": ("backup.zip", backup, "application/zip")},
            data={"confirm": "on"},
            follow_redirects=True,
        )
        assert "Restore complete" in response.text
        restored = ledger.get("/api/transactions").json()
        assert len(restored) == 1
        assert restored[0]["total"] == "-2000.00"
        assert TestClient(stack["stocks"]).get("/api/accounts").json()[0]["name"] == "Fidelity"
        assert len(TestClient(stack["receipts"]).get("/api/documents").json()) == 1

    def test_wrong_zip_rejected(self, logged_in):
        response = logged_in.post(
            "/backup/restore",
            files={"file": ("junk.zip", b"not a zip", "application/zip")},
            data={"confirm": "on"},
            follow_redirects=True,
        )
        assert "not a readable backup zip" in response.text

    def test_incomplete_zip_rejected(self, logged_in):
        buffer = BytesIO()
        with ZipFile(buffer, "w") as archive:
            archive.writestr("ledger.json", "{}")
        response = logged_in.post(
            "/backup/restore",
            files={"file": ("partial.zip", buffer.getvalue(), "application/zip")},
            data={"confirm": "on"},
            follow_redirects=True,
        )
        assert "missing" in response.text
