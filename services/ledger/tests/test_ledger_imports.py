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

    def test_repeats_within_one_file_are_not_duplicates(self, client, profile):
        """Two identical purchases on one day are two real transactions. Dedup
        exists to catch re-importing a statement, not to collapse genuine
        same-day repeats, so the file is never compared against itself."""
        content = "Date,Description,Amount\n08/01/2026,STORE,-10.00\n08/01/2026,STORE,-10.00\n"
        batch = preview(client, profile, content).json()
        assert [r["status"] for r in batch["rows"]] == ["needs_payee", "needs_payee"]
        assert all(r["include"] for r in batch["rows"])
        summary = client.post(f"/api/import/batches/{batch['id']}/commit").json()
        assert summary["created"] == 2

    def test_reimport_of_a_repeated_row_matches_one_for_one(self, client, profile):
        """Re-uploading a statement that legitimately contains the same row
        twice flags exactly two duplicates — not one, and not a fresh pair."""
        content = "Date,Description,Amount\n08/01/2026,STORE,-10.00\n08/01/2026,STORE,-10.00\n"
        first = preview(client, profile, content).json()
        client.post(f"/api/import/batches/{first['id']}/commit")

        second = preview(client, profile, content).json()
        assert second["row_counts"] == {"duplicate": 2}
        summary = client.post(f"/api/import/batches/{second['id']}/commit").json()
        assert summary["created"] == 0

        # A third occurrence on a later statement is new activity, not a repeat
        # of what's already filed.
        third = preview(client, profile, content + "08/01/2026,STORE,-10.00\n").json()
        assert sorted(r["status"] for r in third["rows"]) == [
            "duplicate",
            "duplicate",
            "needs_payee",
        ]

    def test_duplicate_can_be_forced_back_in(self, client, profile):
        content = (SAMPLES / "bank-simple.csv").read_text()
        first = preview(client, profile, content).json()
        client.post(f"/api/import/batches/{first['id']}/commit")
        second = preview(client, profile, content).json()
        dup_row = next(r for r in second["rows"] if r["status"] == "duplicate")
        client.put(f"/api/import/rows/{dup_row['id']}", json={"include": True})
        summary = client.post(f"/api/import/batches/{second['id']}/commit").json()
        assert summary["created"] == 1


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


class TestResolveByDescription:
    """A statement with many unknown rows usually has few distinct merchants;
    the review screen answers each merchant once."""

    CONTENT = (
        "Date,Description,Amount\n"
        "08/01/2026,SAFEWAY STORE 42,-10.00\n"
        "08/02/2026,SAFEWAY STORE 42,-22.00\n"
        "08/03/2026,SAFEWAY  store 42,-31.00\n"
        "08/04/2026,SHELL OIL 771,-40.00\n"
    )

    def test_groups_collapse_rows_by_normalized_description(self, client, profile):
        batch = preview(client, profile, self.CONTENT).json()
        groups = batch["description_groups"]
        assert [g["count"] for g in groups] == [3, 1]
        # Case and spacing differences don't split a merchant into two groups.
        assert groups[0]["count"] == 3
        assert groups[0]["total"] == "-63.00"
        assert all(g["needs_payee"] for g in groups)

    def test_one_resolve_answers_every_matching_row(self, client, profile):
        batch = preview(client, profile, self.CONTENT).json()
        result = client.post(
            f"/api/import/batches/{batch['id']}/resolve",
            json={
                "description": "SAFEWAY STORE 42",
                "create_payee_name": "Safeway",
                "create_category_name": "Food: Groceries",
            },
        ).json()
        assert result["updated"] == 3
        assert all(r["status"] == "ready" for r in result["rows"])
        assert all(r["payee_name"] == "Safeway" for r in result["rows"])

        after = client.get(f"/api/import/batches/{batch['id']}").json()
        assert after["row_counts"] == {"ready": 3, "needs_payee": 1}

    def test_resolving_learns_the_alias_for_next_month(self, client, profile):
        batch = preview(client, profile, self.CONTENT).json()
        client.post(
            f"/api/import/batches/{batch['id']}/resolve",
            json={"description": "SAFEWAY STORE 42", "create_payee_name": "Safeway"},
        )
        client.post(f"/api/import/batches/{batch['id']}/commit")

        later = preview(client, profile, "Date,Description,Amount\n09/01/2026,SAFEWAY STORE 42,-9.00\n").json()
        assert later["rows"][0]["status"] == "ready"
        assert later["rows"][0]["payee_name"] == "Safeway"

    def test_unknown_description_is_rejected(self, client, profile):
        batch = preview(client, profile, self.CONTENT).json()
        response = client.post(
            f"/api/import/batches/{batch['id']}/resolve",
            json={"description": "NOT IN THIS FILE", "create_payee_name": "X"},
        )
        assert response.status_code == 404

    def test_committed_batch_cannot_be_resolved(self, client, profile):
        batch = preview(client, profile, self.CONTENT).json()
        client.post(f"/api/import/batches/{batch['id']}/commit")
        response = client.post(
            f"/api/import/batches/{batch['id']}/resolve",
            json={"description": "SAFEWAY STORE 42", "create_payee_name": "Safeway"},
        )
        assert response.status_code == 422


class TestCategoryCreationDuringImport:
    def test_new_top_level_category_is_created(self, client, profile):
        batch = preview(client, profile, "Date,Description,Amount\n08/01/2026,SHELL,-40.00\n").json()
        row = batch["rows"][0]
        client.put(
            f"/api/import/rows/{row['id']}",
            json={"create_payee_name": "Shell", "create_category_name": "Fuel"},
        )
        categories = client.get("/api/categories").json()
        fuel = next(c for c in categories if c["name"] == "Fuel")
        assert fuel["parent_id"] is None
        assert fuel["kind"] == "expense"  # outflow

    def test_parent_child_path_files_under_an_existing_parent(self, client, profile):
        client.post("/api/categories", json={"name": "Food", "kind": "expense"})
        batch = preview(client, profile, "Date,Description,Amount\n08/01/2026,SAFEWAY,-10.00\n").json()
        client.put(
            f"/api/import/rows/{batch['rows'][0]['id']}",
            json={"create_category_name": "Food: Groceries"},
        )
        categories = client.get("/api/categories").json()
        food = next(c for c in categories if c["name"] == "Food")
        groceries = next(c for c in categories if c["name"] == "Groceries")
        assert groceries["parent_id"] == food["id"]
        assert len([c for c in categories if c["name"] == "Food"]) == 1

    def test_inflow_rows_create_income_categories(self, client, profile):
        batch = preview(client, profile, "Date,Description,Amount\n08/01/2026,ACME PAYROLL,2500.00\n").json()
        client.put(
            f"/api/import/rows/{batch['rows'][0]['id']}",
            json={"create_category_name": "Salary"},
        )
        salary = next(c for c in client.get("/api/categories").json() if c["name"] == "Salary")
        assert salary["kind"] == "income"

    def test_explicit_kind_wins_over_the_inferred_one(self, client, profile):
        batch = preview(client, profile, "Date,Description,Amount\n08/01/2026,REFUND,25.00\n").json()
        client.put(
            f"/api/import/rows/{batch['rows'][0]['id']}",
            json={"create_category_name": "Shopping", "create_category_kind": "expense"},
        )
        shopping = next(c for c in client.get("/api/categories").json() if c["name"] == "Shopping")
        assert shopping["kind"] == "expense"

    def test_existing_category_is_reused_not_duplicated(self, client, profile):
        made = client.post("/api/categories", json={"name": "Fuel", "kind": "expense"}).json()
        batch = preview(client, profile, "Date,Description,Amount\n08/01/2026,SHELL,-40.00\n").json()
        row = client.put(
            f"/api/import/rows/{batch['rows'][0]['id']}",
            json={"create_category_name": "Fuel"},
        ).json()
        assert row["category_id"] == made["id"]
        assert len([c for c in client.get("/api/categories").json() if c["name"] == "Fuel"]) == 1

    def test_created_category_reaches_the_committed_transaction(self, client, profile, checking):
        batch = preview(client, profile, "Date,Description,Amount\n08/01/2026,SHELL,-40.00\n").json()
        client.put(
            f"/api/import/rows/{batch['rows'][0]['id']}",
            json={"create_payee_name": "Shell", "create_category_name": "Auto: Fuel"},
        )
        client.post(f"/api/import/batches/{batch['id']}/commit")
        fuel = next(c for c in client.get("/api/categories").json() if c["path"] == "Auto: Fuel")
        txn = client.get("/api/transactions", params={"account_id": checking["id"]}).json()[0]
        assert txn["splits"][0]["category_id"] == fuel["id"]
        # The new payee remembers it for next time.
        payee = next(p for p in client.get("/api/payees").json() if p["name"] == "Shell")
        assert payee["default_category_id"] == fuel["id"]


class TestFactoryReset:
    def test_reset_empties_the_ledger_but_keeps_it_usable(self, client, profile, checking):
        batch = preview(client, profile, (SAMPLES / "bank-simple.csv").read_text()).json()
        client.post(f"/api/import/batches/{batch['id']}/commit")
        assert client.get("/api/transactions").json() != []

        client.post("/api/reset")
        assert client.get("/api/transactions").json() == []
        assert client.get("/api/accounts").json() == []
        assert client.get("/api/import/profiles").json() == []
        assert client.get("/api/import/batches").json() == []
        # Currencies are re-seeded: without them no account can be created, and
        # a reset is supposed to leave a usable fresh install.
        assert "USD" in {c["code"] for c in client.get("/api/currencies").json()}
        assert client.post("/api/accounts", json={"name": "New", "type": "checking"}).status_code == 200
