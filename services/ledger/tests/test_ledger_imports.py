from pathlib import Path

import pytest

SAMPLES = Path(__file__).resolve().parents[3] / "samples"

PROFILE_CONFIG = {
    "date_column": "date",
    "date_format": "%m/%d/%Y",
    "description_column": "description",
    "amount_column": "amount",
}


@pytest.fixture()
def profile(client, checking):
    return client.post(
        "/api/import/profiles",
        json={"name": "Test Bank", "account_id": checking["id"], "config": PROFILE_CONFIG},
    ).json()


def preview(client, profile, content, filename="test.csv", account_id=None):
    body = {"profile_id": profile["id"], "filename": filename, "content": content}
    if account_id is not None:
        body["account_id"] = account_id
    return client.post("/api/import/preview", json=body)


class TestProfiles:
    def test_config_validation(self, client, checking):
        bad = client.post(
            "/api/import/profiles",
            json={"name": "Bad", "config": {"date_column": "date"}},
        )
        assert bad.status_code == 422
        assert "description_column" in bad.json()["detail"]

        bad_mode = client.post(
            "/api/import/profiles",
            json={
                "name": "Bad2",
                "config": {
                    "date_column": "d",
                    "description_column": "p",
                    "amount_mode": "debit_credit",
                },
            },
        )
        assert bad_mode.status_code == 422

    def test_duplicate_name_conflict(self, client, profile):
        again = client.post(
            "/api/import/profiles",
            json={"name": "Test Bank", "config": PROFILE_CONFIG},
        )
        assert again.status_code == 409


class TestPreviewValidation:
    def test_broken_file_aborts_with_all_errors_and_writes_nothing(self, client, profile):
        content = (SAMPLES / "bank-broken.csv").read_text()
        response = preview(client, profile, content)
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert "nothing was imported" in detail["message"]
        assert len(detail["errors"]) == 2
        assert any("PENDING" in e["raw"] for e in detail["errors"])
        # absolutely nothing written: no batches, no transactions
        assert client.get("/api/import/batches").json() == []
        assert client.get("/api/transactions").json() == []

    def test_valid_file_builds_review_batch(self, client, profile):
        content = (SAMPLES / "bank-simple.csv").read_text()
        response = preview(client, profile, content)
        assert response.status_code == 200
        batch = response.json()
        assert batch["status"] == "review"
        assert batch["total_rows"] == 5
        # nothing hits the register until commit
        assert client.get("/api/transactions").json() == []


class TestClassification:
    def test_unknown_descriptions_need_payee(self, client, profile):
        batch = preview(client, profile, (SAMPLES / "bank-simple.csv").read_text()).json()
        assert batch["row_counts"]["needs_payee"] == 5

    def test_alias_prefills_payee_and_category(self, client, profile, groceries_category):
        safeway = client.post(
            "/api/payees",
            json={"name": "Safeway", "default_category_id": groceries_category["id"]},
        ).json()
        client.post(
            f"/api/payees/{safeway['id']}/aliases",
            json={"pattern": "SAFEWAY", "match_type": "prefix"},
        )
        batch = preview(client, profile, (SAMPLES / "bank-simple.csv").read_text()).json()
        safeway_row = next(r for r in batch["rows"] if "SAFEWAY" in r["description"])
        assert safeway_row["status"] == "ready"
        assert safeway_row["payee_id"] == safeway["id"]
        assert safeway_row["category_id"] == groceries_category["id"]

    def test_transfer_rule_marks_transfer(self, client, profile, savings):
        client.post(
            "/api/transfer-rules",
            json={"pattern": "VENMO", "match_type": "prefix", "account_id": savings["id"]},
        )
        batch = preview(client, profile, (SAMPLES / "bank-simple.csv").read_text()).json()
        venmo_row = next(r for r in batch["rows"] if "VENMO" in r["description"])
        assert venmo_row["status"] == "transfer"
        assert venmo_row["transfer_account_id"] == savings["id"]


class TestDedup:
    def test_second_upload_flags_duplicates(self, client, profile):
        content = (SAMPLES / "bank-simple.csv").read_text()
        first = preview(client, profile, content).json()
        client.post(f"/api/import/batches/{first['id']}/commit")

        second = preview(client, profile, content).json()
        assert second["row_counts"] == {"duplicate": 5}
        assert all(not r["include"] for r in second["rows"])

        # committing the all-duplicates batch creates nothing new
        summary = client.post(f"/api/import/batches/{second['id']}/commit").json()
        assert summary["created"] == 0
        assert summary["skipped"] == 5

    def test_duplicate_can_be_forced_back_in(self, client, profile):
        content = "Date,Description,Amount\n08/01/2026,STORE,-10.00\n08/01/2026,STORE,-10.00\n"
        batch = preview(client, profile, content).json()
        dup_row = next(r for r in batch["rows"] if r["status"] == "duplicate")
        client.put(f"/api/import/rows/{dup_row['id']}", json={"include": True})
        summary = client.post(f"/api/import/batches/{batch['id']}/commit").json()
        assert summary["created"] == 2


class TestCommit:
    def test_commit_creates_cleared_transactions(self, client, profile, checking):
        batch = preview(client, profile, (SAMPLES / "bank-simple.csv").read_text()).json()
        summary = client.post(f"/api/import/batches/{batch['id']}/commit").json()
        assert summary["created"] == 5
        txns = client.get("/api/transactions", params={"account_id": checking["id"]}).json()
        assert len(txns) == 5
        assert all(t["status"] == "cleared" for t in txns)
        assert client.get(f"/api/accounts/{checking['id']}").json()["balance"] == "5665.16"

    def test_commit_twice_rejected(self, client, profile):
        batch = preview(client, profile, (SAMPLES / "bank-simple.csv").read_text()).json()
        client.post(f"/api/import/batches/{batch['id']}/commit")
        assert client.post(f"/api/import/batches/{batch['id']}/commit").status_code == 422

    def test_transfer_rows_become_paired_transfers(self, client, profile, checking, savings):
        client.post(
            "/api/transfer-rules",
            json={"pattern": "VENMO", "match_type": "prefix", "account_id": savings["id"]},
        )
        batch = preview(client, profile, (SAMPLES / "bank-simple.csv").read_text()).json()
        summary = client.post(f"/api/import/batches/{batch['id']}/commit").json()
        assert summary["transfers"] == 1
        transfers = client.get("/api/transactions", params={"kind": "transfer"}).json()
        assert len(transfers) == 2
        savings_leg = next(t for t in transfers if t["account_id"] == savings["id"])
        assert savings_leg["total"] == "60.00"

    def test_debit_credit_profile_end_to_end(self, client, checking):
        profile = client.post(
            "/api/import/profiles",
            json={
                "name": "DC Bank",
                "account_id": checking["id"],
                "config": {
                    "date_column": "posted date",
                    "description_column": "payee",
                    "amount_mode": "debit_credit",
                    "debit_column": "debit",
                    "credit_column": "credit",
                },
            },
        ).json()
        content = (SAMPLES / "bank-debit-credit.csv").read_text()
        batch = preview(client, profile, content).json()
        summary = client.post(f"/api/import/batches/{batch['id']}/commit").json()
        assert summary["created"] == 3
        txns = client.get("/api/transactions", params={"account_id": checking["id"]}).json()
        amounts = sorted(t["total"] for t in txns)
        assert amounts == ["-52.00", "-87.55", "1.23"]


class TestAliasLearning:
    def test_manual_payee_choice_is_learned_for_next_import(
        self, client, profile, groceries_category
    ):
        content = "Date,Description,Amount\n08/03/2026,SAFEWAY #1234 SEATTLE WA,-87.55\n"
        batch = preview(client, profile, content).json()
        row = batch["rows"][0]
        assert row["status"] == "needs_payee"

        # user creates the payee inline; alias learning defaults on
        client.put(
            f"/api/import/rows/{row['id']}",
            json={"create_payee_name": "Safeway", "category_id": groceries_category["id"]},
        )
        client.post(f"/api/import/batches/{batch['id']}/commit")

        # next month, a same-description row maps automatically
        next_month = "Date,Description,Amount\n09/03/2026,SAFEWAY #1234 SEATTLE WA,-92.10\n"
        second = preview(client, profile, next_month).json()
        assert second["rows"][0]["status"] == "ready"
        assert second["rows"][0]["payee_name"] == "Safeway"

    def test_existing_payee_choice_learns_alias_too(self, client, profile, landlord):
        content = "Date,Description,Amount\n08/01/2026,ZELLE TO PROPERTY MGMT,-2000.00\n"
        batch = preview(client, profile, content).json()
        client.put(f"/api/import/rows/{batch['rows'][0]['id']}", json={"payee_id": landlord["id"]})
        client.post(f"/api/import/batches/{batch['id']}/commit")

        aliases = client.get(f"/api/payees/{landlord['id']}/aliases").json()
        assert any(a["pattern"] == "ZELLE TO PROPERTY MGMT" for a in aliases)


class TestSeedingNewAccount:
    def test_import_into_fresh_account_sets_history(self, client, profile):
        fresh = client.post("/api/accounts", json={"name": "New Card", "type": "credit_card"}).json()
        content = "Date,Description,Amount\n07/01/2026,OLD CHARGE,-100.00\n"
        batch = preview(client, profile, content, account_id=fresh["id"]).json()
        client.post(f"/api/import/batches/{batch['id']}/commit")
        assert client.get(f"/api/accounts/{fresh['id']}").json()["balance"] == "-100.00"


class TestAbort:
    def test_abort_keeps_register_clean(self, client, profile):
        batch = preview(client, profile, (SAMPLES / "bank-simple.csv").read_text()).json()
        client.post(f"/api/import/batches/{batch['id']}/abort")
        assert client.get("/api/transactions").json() == []
        assert client.post(f"/api/import/batches/{batch['id']}/commit").status_code == 422
