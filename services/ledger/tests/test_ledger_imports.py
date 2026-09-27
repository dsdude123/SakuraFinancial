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
        assert "description column" in bad.json()["detail"]

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


class TestSequenceResetSafety:
    """reset_sequences once assumed every table has an id and blew up on
    Postgres against `currencies`, which is keyed by its ISO code. SQLite skips
    the whole function, so this asserts the table filtering directly."""

    def test_id_less_tables_are_filtered_out(self):
        from ledger_service.routers.exports import CORE_TABLES, tables_with_serial_id

        selected = tables_with_serial_id(CORE_TABLES)
        assert "currencies" not in selected
        assert "transactions" in selected
        assert "accounts" in selected

    def test_every_table_the_reset_sweeps_is_either_filtered_or_has_an_id(self):
        from ledger_service.db import Base
        from ledger_service.routers.exports import CORE_TABLES, tables_with_serial_id

        selected = set(tables_with_serial_id(CORE_TABLES))
        for table in CORE_TABLES:
            columns = Base.metadata.tables[table].columns
            assert (table in selected) == ("id" in columns), table

    def test_unknown_table_names_are_ignored_not_crashed_on(self):
        from ledger_service.routers.exports import tables_with_serial_id

        assert tables_with_serial_id(["not_a_real_table"]) == []


class TestRegexPayeeAliases:
    """One merchant, a different reference number every statement line."""

    EXPEDIA = (
        "Date,Description,Amount\n"
        "08/01/2026,EXPEDIA INC. 00000000000000004085 - DIR DEP,-120.00\n"
        "08/09/2026,EXPEDIA INC. 00000000000000009912 - DIR DEP,-340.00\n"
        "08/17/2026,EXPEDIA INC. 00000000000000000001 - DIR DEP,-55.00\n"
    )

    def test_one_regex_claims_every_variant(self, client, profile):
        payee = client.post("/api/payees", json={"name": "Expedia"}).json()
        response = client.post(
            f"/api/payees/{payee['id']}/aliases",
            json={"pattern": r"^EXPEDIA INC\. \d+ - DIR DEP$", "match_type": "regex"},
        )
        assert response.status_code == 200
        batch = preview(client, profile, self.EXPEDIA).json()
        assert batch["row_counts"] == {"ready": 3}
        assert {r["payee_name"] for r in batch["rows"]} == {"Expedia"}

    def test_regex_pattern_is_stored_verbatim_not_upper_cased(self, client, profile):
        """Normalizing a regex would turn \\d into \\D and invert it."""
        payee = client.post("/api/payees", json={"name": "Expedia"}).json()
        alias = client.post(
            f"/api/payees/{payee['id']}/aliases",
            json={"pattern": r"expedia inc\. \d+", "match_type": "regex"},
        ).json()
        assert alias["pattern"] == r"expedia inc\. \d+"
        # Stored lower case, still matches the upper-cased description.
        batch = preview(client, profile, self.EXPEDIA).json()
        assert batch["row_counts"] == {"ready": 3}

    def test_a_bad_regex_is_rejected_when_it_is_typed(self, client):
        payee = client.post("/api/payees", json={"name": "X"}).json()
        response = client.post(
            f"/api/payees/{payee['id']}/aliases",
            json={"pattern": "EXPEDIA (unclosed", "match_type": "regex"},
        )
        assert response.status_code == 422
        assert "valid regular expression" in response.json()["detail"]

    def test_bank_fees_with_trailing_numbers_collapse_to_one_payee(self, client, profile):
        fees = client.post("/api/payees", json={"name": "Big Bank"}).json()
        client.post(
            f"/api/payees/{fees['id']}/aliases",
            json={"pattern": r"^FOREIGN TRANSACTION FEE( \d+)?$", "match_type": "regex"},
        )
        content = (
            "Date,Description,Amount\n"
            "08/01/2026,Foreign Transaction Fee 76,-1.20\n"
            "08/02/2026,Foreign Transaction Fee 77,-0.94\n"
            "08/03/2026,Foreign Transaction Fee,-2.00\n"
        )
        batch = preview(client, profile, content).json()
        assert batch["row_counts"] == {"ready": 3}

    def test_exact_alias_still_beats_a_regex(self, client, profile):
        loose = client.post("/api/payees", json={"name": "Loose"}).json()
        exact = client.post("/api/payees", json={"name": "Exact"}).json()
        client.post(
            f"/api/payees/{loose['id']}/aliases",
            json={"pattern": "EXPEDIA.*", "match_type": "regex"},
        )
        client.post(
            f"/api/payees/{exact['id']}/aliases",
            json={"pattern": "EXPEDIA INC. 00000000000000004085 - DIR DEP", "match_type": "exact"},
        )
        batch = preview(client, profile, self.EXPEDIA).json()
        first = next(r for r in batch["rows"] if "4085" in r["description"])
        assert first["payee_name"] == "Exact"
        rest = [r for r in batch["rows"] if "4085" not in r["description"]]
        assert {r["payee_name"] for r in rest} == {"Loose"}

    def test_regex_beats_a_loose_contains_rule(self, client, profile):
        broad = client.post("/api/payees", json={"name": "Broad"}).json()
        precise = client.post("/api/payees", json={"name": "Precise"}).json()
        client.post(
            f"/api/payees/{broad['id']}/aliases",
            json={"pattern": "INC", "match_type": "contains"},
        )
        client.post(
            f"/api/payees/{precise['id']}/aliases",
            json={"pattern": r"EXPEDIA INC\. \d+", "match_type": "regex"},
        )
        batch = preview(client, profile, self.EXPEDIA).json()
        assert {r["payee_name"] for r in batch["rows"]} == {"Precise"}


class TestTransferCounterpartDedup:
    """Both sides of one transfer arrive on two different statements, worded
    differently. The second import must not book it again."""

    def rules(self, client, checking, savings, days=5):
        client.post(
            "/api/transfer-rules",
            json={"pattern": "TO SAVINGS", "match_type": "contains",
                  "account_id": savings["id"], "match_days": days},
        )
        client.post(
            "/api/transfer-rules",
            json={"pattern": "FROM CHECKING", "match_type": "contains",
                  "account_id": checking["id"], "match_days": days},
        )

    def savings_profile(self, client, savings):
        return client.post(
            "/api/import/profiles",
            json={"name": "Savings Bank", "account_id": savings["id"], "config": PROFILE_CONFIG},
        ).json()

    def test_other_side_is_flagged_not_double_booked(
        self, client, profile, checking, savings
    ):
        self.rules(client, checking, savings)
        out = preview(client, profile, "Date,Description,Amount\n08/03/2026,TO SAVINGS,-500.00\n").json()
        client.post(f"/api/import/batches/{out['id']}/commit")
        assert len(client.get("/api/transactions").json()) == 2  # both legs

        other = self.savings_profile(client, savings)
        # Same money, different words, and it cleared three days later.
        incoming = preview(
            client, other, "Date,Description,Amount\n08/06/2026,FROM CHECKING,500.00\n"
        ).json()
        row = incoming["rows"][0]
        assert row["status"] == "counterpart"
        assert row["include"] is False

        summary = client.post(f"/api/import/batches/{incoming['id']}/commit").json()
        assert summary["created"] == 0
        assert summary["transfers"] == 0
        assert len(client.get("/api/transactions").json()) == 2  # still just the pair

    def test_outside_the_window_it_is_a_real_second_transfer(
        self, client, profile, checking, savings
    ):
        self.rules(client, checking, savings, days=2)
        out = preview(client, profile, "Date,Description,Amount\n08/03/2026,TO SAVINGS,-500.00\n").json()
        client.post(f"/api/import/batches/{out['id']}/commit")

        other = self.savings_profile(client, savings)
        late = preview(
            client, other, "Date,Description,Amount\n08/20/2026,FROM CHECKING,500.00\n"
        ).json()
        assert late["rows"][0]["status"] == "transfer"

    def test_a_different_amount_is_not_the_same_transfer(
        self, client, profile, checking, savings
    ):
        self.rules(client, checking, savings)
        out = preview(client, profile, "Date,Description,Amount\n08/03/2026,TO SAVINGS,-500.00\n").json()
        client.post(f"/api/import/batches/{out['id']}/commit")

        other = self.savings_profile(client, savings)
        different = preview(
            client, other, "Date,Description,Amount\n08/04/2026,FROM CHECKING,250.00\n"
        ).json()
        assert different["rows"][0]["status"] == "transfer"

    def test_two_identical_transfers_match_one_for_one(
        self, client, profile, checking, savings
    ):
        """Two £500 moves in the same week are two transfers, and the other
        statement's two rows must claim one leg each - not both the same one."""
        self.rules(client, checking, savings)
        out = preview(
            client,
            profile,
            "Date,Description,Amount\n08/03/2026,TO SAVINGS,-500.00\n"
            "08/04/2026,TO SAVINGS,-500.00\n",
        ).json()
        client.post(f"/api/import/batches/{out['id']}/commit")

        other = self.savings_profile(client, savings)
        incoming = preview(
            client,
            other,
            "Date,Description,Amount\n08/05/2026,FROM CHECKING,500.00\n"
            "08/06/2026,FROM CHECKING,500.00\n",
        ).json()
        assert [r["status"] for r in incoming["rows"]] == ["counterpart", "counterpart"]

        # A third one really is new money.
        third = preview(
            client, other, "Date,Description,Amount\n08/05/2026,FROM CHECKING,500.00\n"
            "08/06/2026,FROM CHECKING,500.00\n08/07/2026,FROM CHECKING,500.00\n"
        ).json()
        assert [r["status"] for r in third["rows"]].count("transfer") == 1

    def test_an_unrelated_transfer_of_the_same_size_is_not_swallowed(
        self, client, profile, checking, savings, ledger_third_account
    ):
        """The counterpart must sit in the account the rule points at."""
        third = ledger_third_account
        client.post(
            "/api/transfer-rules",
            json={"pattern": "TO THIRD", "match_type": "contains", "account_id": third["id"]},
        )
        out = preview(client, profile, "Date,Description,Amount\n08/03/2026,TO THIRD,-500.00\n").json()
        client.post(f"/api/import/batches/{out['id']}/commit")

        # Savings imports a 500 credit whose rule points at checking - the
        # existing transfer pairs checking with the third account, not savings.
        client.post(
            "/api/transfer-rules",
            json={"pattern": "FROM CHECKING", "match_type": "contains",
                  "account_id": checking["id"]},
        )
        other = self.savings_profile(client, savings)
        incoming = preview(
            client, other, "Date,Description,Amount\n08/04/2026,FROM CHECKING,500.00\n"
        ).json()
        assert incoming["rows"][0]["status"] == "transfer"

    def test_match_days_round_trips_through_the_api(self, client, savings):
        rule = client.post(
            "/api/transfer-rules",
            json={"pattern": "X", "match_type": "contains",
                  "account_id": savings["id"], "match_days": 9},
        ).json()
        assert rule["match_days"] == 9
        assert client.get("/api/transfer-rules").json()[0]["match_days"] == 9
        updated = client.put(
            f"/api/transfer-rules/{rule['id']}",
            json={"pattern": "X", "match_type": "contains",
                  "account_id": savings["id"], "match_days": 0},
        ).json()
        assert updated["match_days"] == 0

    def test_negative_window_rejected(self, client, savings):
        response = client.post(
            "/api/transfer-rules",
            json={"pattern": "X", "match_type": "contains",
                  "account_id": savings["id"], "match_days": -1},
        )
        assert response.status_code == 422


class TestAccountPayeeForBankCharges:
    """Fees and interest are levied by the institution, so the account's own
    name is the payee for them."""

    def test_a_cash_flow_account_gets_a_same_named_payee(self, client):
        client.post("/api/accounts", json={"name": "Sapphire Card", "type": "credit_card"})
        names = {p["name"] for p in client.get("/api/payees").json()}
        assert "Sapphire Card" in names

    def test_every_cash_flow_type_gets_one(self, client):
        for name, kind in (
            ("A Checking", "checking"),
            ("A Savings", "savings"),
            ("A Card", "credit_card"),
            ("A Wallet", "cash"),
        ):
            client.post("/api/accounts", json={"name": name, "type": kind})
        names = {p["name"] for p in client.get("/api/payees").json()}
        assert {"A Checking", "A Savings", "A Card", "A Wallet"} <= names

    def test_asset_accounts_do_not_get_one(self, client):
        client.post("/api/accounts", json={"name": "The Car", "type": "asset"})
        assert "The Car" not in {p["name"] for p in client.get("/api/payees").json()}

    def test_an_existing_payee_of_that_name_is_reused(self, client):
        made = client.post("/api/payees", json={"name": "Big Bank"}).json()
        client.post("/api/accounts", json={"name": "Big Bank", "type": "checking"})
        matching = [p for p in client.get("/api/payees").json() if p["name"] == "Big Bank"]
        assert [p["id"] for p in matching] == [made["id"]]

    def test_the_account_payee_can_take_the_fee_transactions(self, client, profile, checking):
        """End to end: a fee row books against the account's own payee."""
        bank = next(p for p in client.get("/api/payees").json() if p["name"] == "Checking")
        client.post(
            f"/api/payees/{bank['id']}/aliases",
            json={"pattern": r"^FOREIGN TRANSACTION FEE( \d+)?$", "match_type": "regex"},
        )
        batch = preview(
            client, profile,
            "Date,Description,Amount\n08/01/2026,Foreign Transaction Fee 76,-1.20\n",
        ).json()
        assert batch["rows"][0]["payee_name"] == "Checking"


class TestAccountEditing:
    def test_name_note_and_opening_balance_are_editable(self, client, checking):
        updated = client.put(
            f"/api/accounts/{checking['id']}",
            json={"name": "Everyday", "opening_balance": "250.00", "note": "joint"},
        ).json()
        assert updated["name"] == "Everyday"
        assert updated["opening_balance"] == "250.00"
        assert updated["note"] == "joint"

    def test_opening_balance_correction_moves_the_running_balance(self, client, checking):
        """The reason this matters: a card export with no starting balance
        means guessing, then correcting once you can compare to the real card."""
        client.post(
            "/api/transactions",
            json={
                "account_id": checking["id"],
                "date": "2026-08-01",
                "splits": [{"category_id": None, "amount": "-40.00"}],
            },
        )
        before = client.get(f"/api/accounts/{checking['id']}").json()["balance"]
        assert before == "960.00"
        client.put(f"/api/accounts/{checking['id']}", json={"opening_balance": "-1300.00"})
        after = client.get(f"/api/accounts/{checking['id']}").json()
        assert after["balance"] == "-1340.00"

    def test_type_is_editable(self, client, checking):
        updated = client.put(
            f"/api/accounts/{checking['id']}", json={"type": "credit_card"}
        ).json()
        assert updated["type"] == "credit_card"

    def test_an_unknown_type_is_rejected(self, client, checking):
        response = client.put(f"/api/accounts/{checking['id']}", json={"type": "wishful"})
        assert response.status_code == 422

    def test_currency_can_change_while_the_account_is_empty(self, client, checking):
        updated = client.put(
            f"/api/accounts/{checking['id']}", json={"currency_code": "cad"}
        ).json()
        assert updated["currency_code"] == "CAD"

    def test_currency_is_refused_once_amounts_are_posted(self, client, checking):
        client.post(
            "/api/transactions",
            json={
                "account_id": checking["id"],
                "date": "2026-08-01",
                "splits": [{"category_id": None, "amount": "-40.00"}],
            },
        )
        response = client.put(
            f"/api/accounts/{checking['id']}", json={"currency_code": "CAD"}
        )
        assert response.status_code == 409
        assert "reinterpret" in response.json()["detail"]

    def test_unknown_currency_rejected(self, client, checking):
        response = client.put(
            f"/api/accounts/{checking['id']}", json={"currency_code": "ZZZ"}
        )
        assert response.status_code == 422


class TestReclassifyMidReview:
    """The alias you write *after* uploading. Adding it should catch the batch
    up without discarding it and re-uploading the file."""

    CONTENT = (
        "Date,Description,Amount\n"
        "08/01/2026,EXPEDIA INC. 00000000000000004085 - DIR DEP,-120.00\n"
        "08/02/2026,EXPEDIA INC. 00000000000000009912 - DIR DEP,-340.00\n"
        "08/03/2026,QFC,-60.61\n"
    )

    def test_a_new_regex_claims_rows_already_in_review(self, client, profile):
        batch = preview(client, profile, self.CONTENT).json()
        assert batch["row_counts"] == {"needs_payee": 3}

        payee = client.post("/api/payees", json={"name": "Expedia"}).json()
        client.post(
            f"/api/payees/{payee['id']}/aliases",
            json={"pattern": r"^EXPEDIA INC\. \d+ - DIR DEP$", "match_type": "regex"},
        )
        result = client.post(f"/api/import/batches/{batch['id']}/reclassify").json()
        assert result["changed"] == 2
        assert result["row_counts"] == {"ready": 2, "needs_payee": 1}

        after = client.get(f"/api/import/batches/{batch['id']}").json()
        assert {r["payee_name"] for r in after["rows"] if r["payee_name"]} == {"Expedia"}

    def test_answers_already_given_are_not_undone(self, client, profile):
        batch = preview(client, profile, self.CONTENT).json()
        qfc_row = next(r for r in batch["rows"] if r["description"] == "QFC")
        client.put(
            f"/api/import/rows/{qfc_row['id']}", json={"create_payee_name": "QFC Groceries"}
        )
        # A regex that would also claim that row, added afterwards.
        other = client.post("/api/payees", json={"name": "Wrong"}).json()
        client.post(
            f"/api/payees/{other['id']}/aliases", json={"pattern": ".*", "match_type": "regex"}
        )
        client.post(f"/api/import/batches/{batch['id']}/reclassify")

        after = client.get(f"/api/import/batches/{batch['id']}").json()
        kept = next(r for r in after["rows"] if r["description"] == "QFC")
        assert kept["payee_name"] == "QFC Groceries"

    def test_a_new_transfer_rule_is_picked_up_too(self, client, profile, savings):
        batch = preview(
            client, profile, "Date,Description,Amount\n08/01/2026,TO SAVINGS,-500.00\n"
        ).json()
        assert batch["rows"][0]["status"] == "needs_payee"
        client.post(
            "/api/transfer-rules",
            json={"pattern": "TO SAVINGS", "match_type": "contains", "account_id": savings["id"]},
        )
        client.post(f"/api/import/batches/{batch['id']}/reclassify")
        after = client.get(f"/api/import/batches/{batch['id']}").json()
        assert after["rows"][0]["status"] == "transfer"
        assert after["rows"][0]["transfer_account_id"] == savings["id"]

    def test_reclassifying_changes_nothing_when_no_rules_changed(self, client, profile):
        batch = preview(client, profile, self.CONTENT).json()
        assert client.post(f"/api/import/batches/{batch['id']}/reclassify").json()["changed"] == 0

    def test_a_committed_batch_cannot_be_reclassified(self, client, profile):
        batch = preview(client, profile, self.CONTENT).json()
        client.post(f"/api/import/batches/{batch['id']}/commit")
        response = client.post(f"/api/import/batches/{batch['id']}/reclassify")
        assert response.status_code == 422


class TestDescriptionBecomesThePayee:
    def test_unanswered_rows_are_filed_under_their_description(self, client, profile):
        batch = preview(
            client, profile,
            "Date,Description,Amount\n08/01/2026,QFC,-60.61\n08/02/2026,Service Charge,-6.00\n",
        ).json()
        client.post(f"/api/import/batches/{batch['id']}/commit")
        names = {p["name"] for p in client.get("/api/payees").json()}
        assert {"QFC", "Service Charge"} <= names
        txns = client.get("/api/transactions").json()
        assert {t["payee_name"] for t in txns} == {"QFC", "Service Charge"}

    def test_an_existing_payee_of_that_name_is_reused(self, client, profile):
        made = client.post("/api/payees", json={"name": "QFC"}).json()
        batch = preview(client, profile, "Date,Description,Amount\n08/01/2026,QFC,-60.61\n").json()
        client.post(f"/api/import/batches/{batch['id']}/commit")
        matching = [p for p in client.get("/api/payees").json() if p["name"] == "QFC"]
        assert [p["id"] for p in matching] == [made["id"]]

    def test_a_chosen_payee_still_wins(self, client, profile):
        batch = preview(client, profile, "Date,Description,Amount\n08/01/2026,QFC,-60.61\n").json()
        client.put(
            f"/api/import/rows/{batch['rows'][0]['id']}",
            json={"create_payee_name": "Quality Food Centers"},
        )
        client.post(f"/api/import/batches/{batch['id']}/commit")
        assert client.get("/api/transactions").json()[0]["payee_name"] == "Quality Food Centers"

    def test_it_can_be_turned_off(self, client, profile):
        batch = preview(client, profile, "Date,Description,Amount\n08/01/2026,QFC,-60.61\n").json()
        client.post(
            f"/api/import/batches/{batch['id']}/commit",
            params={"name_payees_from_descriptions": False},
        )
        assert client.get("/api/transactions").json()[0]["payee_name"] is None
        assert "QFC" not in {p["name"] for p in client.get("/api/payees").json()}

    def test_transfers_never_get_a_description_payee(self, client, profile, savings):
        client.post(
            "/api/transfer-rules",
            json={"pattern": "TO SAVINGS", "match_type": "contains", "account_id": savings["id"]},
        )
        batch = preview(
            client, profile, "Date,Description,Amount\n08/01/2026,TO SAVINGS,-500.00\n"
        ).json()
        client.post(f"/api/import/batches/{batch['id']}/commit")
        assert "TO SAVINGS" not in {p["name"] for p in client.get("/api/payees").json()}
