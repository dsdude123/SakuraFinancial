import pytest


@pytest.fixture()
def utilities_category(client):
    return client.post("/api/categories", json={"name": "Utilities", "kind": "expense"}).json()


@pytest.fixture()
def power_payee(client, utilities_category):
    return client.post(
        "/api/payees",
        json={"name": "Puget Sound Energy", "default_category_id": utilities_category["id"]},
    ).json()


@pytest.fixture()
def fixed_bill(client, power_payee, utilities_category):
    """A fixed $142.19 monthly power bill due on the 5th."""
    return client.post(
        "/api/bills",
        json={
            "name": "Electric",
            "payee_id": power_payee["id"],
            "category_id": utilities_category["id"],
            "frequency": "monthly",
            "amount": "142.19",
            "is_variable": False,
            "next_due": "2026-08-05",
        },
    ).json()


@pytest.fixture()
def insurance_bill(client, rent_category):
    payee = client.post("/api/payees", json={"name": "Allstate"}).json()
    return client.post(
        "/api/bills",
        json={
            "name": "Car Insurance",
            "payee_id": payee["id"],
            "category_id": rent_category["id"],
            "frequency": "annual",
            "amount": "1200.00",
            "is_variable": False,
            "next_due": "2026-12-15",
        },
    ).json()


class TestSchedules:
    def test_monthly_bill_generates_a_year_of_occurrences(self, client, fixed_bill):
        detail = client.get(f"/api/bills/{fixed_bill['id']}").json()
        assert len(detail["occurrences"]) >= 12
        assert detail["occurrences"][0]["due_date"] == "2026-08-05"
        assert detail["occurrences"][1]["due_date"] == "2026-09-05"

    def test_annual_bill_generates_upcoming_occurrence(self, client, insurance_bill):
        detail = client.get(f"/api/bills/{insurance_bill['id']}").json()
        due_dates = [o["due_date"] for o in detail["occurrences"]]
        assert "2026-12-15" in due_dates

    def test_month_end_clamping(self, client, power_payee):
        bill = client.post(
            "/api/bills",
            json={
                "name": "EOM",
                "payee_id": power_payee["id"],
                "frequency": "monthly",
                "amount": "10.00",
                "next_due": "2026-08-31",
            },
        ).json()
        due_dates = [o["due_date"] for o in client.get(f"/api/bills/{bill['id']}").json()["occurrences"]]
        assert "2026-09-30" in due_dates  # not September 31st

    def test_bad_frequency_rejected(self, client, power_payee):
        response = client.post(
            "/api/bills",
            json={
                "name": "X",
                "payee_id": power_payee["id"],
                "frequency": "fortnightly",
                "amount": "1.00",
                "next_due": "2026-08-01",
            },
        )
        assert response.status_code == 422


class TestMatching:
    def test_exact_fixed_amount_marks_paid(self, client, checking, fixed_bill, power_payee, make_txn):
        txn = make_txn(
            checking["id"], "2026-08-05", "-142.19", payee_id=power_payee["id"]
        )
        assert txn["bill_matches"][0]["status"] == "paid"
        assert txn["bill_matches"][0]["bill_name"] == "Electric"

    def test_nearby_date_still_matches(self, client, checking, fixed_bill, power_payee, make_txn):
        txn = make_txn(checking["id"], "2026-08-09", "-142.19", payee_id=power_payee["id"])
        assert txn["bill_matches"][0]["status"] == "paid"

    def test_far_date_does_not_match(self, client, checking, fixed_bill, power_payee, make_txn):
        txn = make_txn(checking["id"], "2026-08-25", "-142.19", payee_id=power_payee["id"])
        assert txn["bill_matches"] == []

    def test_different_payee_does_not_match(self, client, checking, fixed_bill, landlord, make_txn):
        txn = make_txn(checking["id"], "2026-08-05", "-142.19", payee_id=landlord["id"])
        assert txn["bill_matches"] == []

    def test_variable_bill_accepts_any_amount(self, client, checking, power_payee, make_txn):
        client.post(
            "/api/bills",
            json={
                "name": "Water",
                "payee_id": power_payee["id"],
                "frequency": "monthly",
                "amount": "80.00",
                "is_variable": True,
                "next_due": "2026-08-05",
            },
        )
        txn = make_txn(checking["id"], "2026-08-05", "-97.42", payee_id=power_payee["id"])
        assert txn["bill_matches"][0]["status"] == "paid"
        assert txn["bill_matches"][0]["actual_amount"] == "97.42"

    def test_fixed_bill_different_amount_flags_review(
        self, client, checking, fixed_bill, power_payee, make_txn
    ):
        """The core prompt flow: fixed bill, new amount -> amount_review."""
        txn = make_txn(checking["id"], "2026-08-05", "-150.00", payee_id=power_payee["id"])
        match = txn["bill_matches"][0]
        assert match["status"] == "amount_review"
        assert match["expected_amount"] == "142.19"
        assert match["actual_amount"] == "150.00"

    def test_inflow_never_matches_bills(self, client, checking, fixed_bill, power_payee, make_txn):
        txn = make_txn(checking["id"], "2026-08-05", "142.19", payee_id=power_payee["id"])
        assert txn["bill_matches"] == []


class TestAmountReviewResolution:
    def make_review(self, client, checking, power_payee, make_txn):
        txn = make_txn(checking["id"], "2026-08-05", "-150.00", payee_id=power_payee["id"])
        return txn["bill_matches"][0]

    def test_update_bill_adopts_new_amount_everywhere(
        self, client, checking, fixed_bill, power_payee, make_txn
    ):
        match = self.make_review(client, checking, power_payee, make_txn)
        result = client.post(
            f"/api/bills/occurrences/{match['id']}/resolve", json={"action": "update_bill"}
        ).json()
        assert result["status"] == "paid"

        bill = client.get(f"/api/bills/{fixed_bill['id']}").json()
        assert bill["amount"] == "150.00"
        upcoming = [o for o in bill["occurrences"] if o["status"] == "upcoming"]
        assert all(o["expected_amount"] == "150.00" for o in upcoming)

    def test_keep_leaves_bill_amount(self, client, checking, fixed_bill, power_payee, make_txn):
        match = self.make_review(client, checking, power_payee, make_txn)
        result = client.post(
            f"/api/bills/occurrences/{match['id']}/resolve", json={"action": "keep"}
        ).json()
        assert result["status"] == "paid"
        assert client.get(f"/api/bills/{fixed_bill['id']}").json()["amount"] == "142.19"

    def test_resolving_non_review_rejected(self, client, checking, fixed_bill, power_payee, make_txn):
        txn = make_txn(checking["id"], "2026-08-05", "-142.19", payee_id=power_payee["id"])
        match = txn["bill_matches"][0]
        response = client.post(
            f"/api/bills/occurrences/{match['id']}/resolve", json={"action": "keep"}
        )
        assert response.status_code == 422


class TestImportIntegration:
    def test_import_commit_matches_bills_and_surfaces_prompts(
        self, client, checking, fixed_bill, power_payee, utilities_category
    ):
        """User requirement: CSV imports go through bill matching, and amount
        changes on fixed bills prompt exactly like manual entry."""
        client.post(
            f"/api/payees/{power_payee['id']}/aliases",
            json={"pattern": "PUGET SOUND ENERGY", "match_type": "prefix"},
        )
        profile = client.post(
            "/api/import/profiles",
            json={
                "name": "Bank",
                "account_id": checking["id"],
                "config": {
                    "date_column": "date",
                    "description_column": "description",
                    "amount_column": "amount",
                },
            },
        ).json()
        content = "Date,Description,Amount\n08/05/2026,PUGET SOUND ENERGY BILLPAY,-150.00\n"
        batch = client.post(
            "/api/import/preview",
            json={"profile_id": profile["id"], "filename": "aug.csv", "content": content},
        ).json()
        summary = client.post(f"/api/import/batches/{batch['id']}/commit").json()

        assert len(summary["amount_review"]) == 1
        prompt = summary["amount_review"][0]
        assert prompt["bill_name"] == "Electric"
        assert prompt["expected_amount"] == "142.19"
        assert prompt["actual_amount"] == "150.00"

        # the prompt stays visible on the bills overview until resolved
        pending = client.get("/api/bills/occurrences", params={"status": "amount_review"}).json()
        assert len(pending) == 1


class TestAccrual:
    def test_annual_bill_reported_as_monthly_load(self, client, insurance_bill, fixed_bill):
        summary = client.get("/api/bills/accrual", params={"on": "2026-08-09"}).json()
        insurance = next(b for b in summary["bills"] if b["name"] == "Car Insurance")
        assert insurance["monthly_load"] == "100.00"
        electric = next(b for b in summary["bills"] if b["name"] == "Electric")
        assert electric["monthly_load"] == "142.19"
        assert summary["total_monthly_load"] == "242.19"

    def test_set_aside_progress_for_annual_bill(self, client, insurance_bill):
        summary = client.get("/api/bills/accrual", params={"on": "2026-08-09"}).json()
        insurance = next(b for b in summary["bills"] if b["name"] == "Car Insurance")
        # as of 2026-08, due 2026-12 -> 4 months remain of the 12-month cycle,
        # so 8 months' worth should already be set aside
        assert insurance["months_until_due"] == 4
        assert insurance["set_aside_target"] == "800.00"

    def test_by_category_grouping(self, client, insurance_bill, fixed_bill):
        summary = client.get("/api/bills/accrual", params={"on": "2026-08-09"}).json()
        assert len(summary["by_category"]) == 2
        total = sum(float(b["monthly_load"]) for b in summary["by_category"])
        assert abs(total - 242.19) < 0.001


class TestOccurrenceManagement:
    def test_skip(self, client, fixed_bill):
        occ = client.get(f"/api/bills/{fixed_bill['id']}").json()["occurrences"][0]
        result = client.post(f"/api/bills/occurrences/{occ['id']}/skip").json()
        assert result["status"] == "skipped"

    def test_unmatch_resets(self, client, checking, fixed_bill, power_payee, make_txn):
        txn = make_txn(checking["id"], "2026-08-05", "-150.00", payee_id=power_payee["id"])
        match = txn["bill_matches"][0]
        result = client.post(f"/api/bills/occurrences/{match['id']}/unmatch").json()
        assert result["status"] == "upcoming"
        assert result["matched_transaction_id"] is None
        assert result["expected_amount"] == "142.19"

    def test_amount_update_propagates_to_upcoming(self, client, fixed_bill):
        client.put(f"/api/bills/{fixed_bill['id']}", json={"amount": "160.00"})
        detail = client.get(f"/api/bills/{fixed_bill['id']}").json()
        upcoming = [o for o in detail["occurrences"] if o["status"] == "upcoming"]
        assert all(o["expected_amount"] == "160.00" for o in upcoming)


class TestExportRoundTrip:
    def test_bills_survive_export_import(self, client, fixed_bill):
        exported = client.get("/api/export").json()
        assert len(exported["bills"]) == 1
        assert len(exported["bill_occurrences"]) >= 12
        client.post("/api/import", json=exported)
        bills = client.get("/api/bills").json()
        assert bills[0]["name"] == "Electric"
        occurrences = client.get(f"/api/bills/{bills[0]['id']}").json()["occurrences"]
        assert len(occurrences) >= 12
