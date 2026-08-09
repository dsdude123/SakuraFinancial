class TestTransactions:
    def test_simple_expense(self, client, checking, groceries_category, make_txn):
        txn = make_txn(checking["id"], "2026-08-02", "-45.10", groceries_category["id"])
        assert txn["total"] == "-45.10"
        assert txn["splits"][0]["category_name"] == "Groceries"

    def test_multi_category_split_single_transaction(
        self, client, checking, groceries_category, rent_category, make_txn
    ):
        """One Costco run, several categories, ONE transaction — the register
        and the account balance see a single -100.00 entry."""
        txn = make_txn(
            checking["id"],
            "2026-08-02",
            splits=[
                {"category_id": groceries_category["id"], "amount": "-70.00"},
                {"category_id": rent_category["id"], "amount": "-30.00"},
            ],
        )
        assert txn["total"] == "-100.00"
        assert len(txn["splits"]) == 2
        listed = client.get("/api/transactions", params={"account_id": checking["id"]}).json()
        assert len(listed) == 1

    def test_empty_splits_rejected(self, client, checking):
        response = client.post(
            "/api/transactions",
            json={"account_id": checking["id"], "date": "2026-08-01", "splits": []},
        )
        assert response.status_code == 422

    def test_unknown_category_rejected(self, client, checking):
        response = client.post(
            "/api/transactions",
            json={
                "account_id": checking["id"],
                "date": "2026-08-01",
                "splits": [{"category_id": 999, "amount": "-1.00"}],
            },
        )
        assert response.status_code == 422

    def test_update_fields_and_status(self, client, checking, groceries_category, make_txn):
        txn = make_txn(checking["id"], "2026-08-02", "-45.10", groceries_category["id"])
        response = client.put(
            f"/api/transactions/{txn['id']}",
            json={"memo": "weekly shop", "status": "cleared"},
        )
        assert response.json()["memo"] == "weekly shop"
        assert response.json()["status"] == "cleared"

    def test_replace_splits_must_preserve_total(
        self, client, checking, groceries_category, rent_category, make_txn
    ):
        txn = make_txn(checking["id"], "2026-08-02", "-100.00", groceries_category["id"])
        bad = client.put(
            f"/api/transactions/{txn['id']}/splits",
            json={"splits": [{"category_id": rent_category["id"], "amount": "-90.00"}]},
        )
        assert bad.status_code == 422
        assert "sum to the transaction total" in bad.json()["detail"]

        good = client.put(
            f"/api/transactions/{txn['id']}/splits",
            json={
                "splits": [
                    {"category_id": rent_category["id"], "amount": "-60.00"},
                    {"category_id": groceries_category["id"], "amount": "-40.00"},
                ]
            },
        )
        assert good.status_code == 200
        assert good.json()["total"] == "-100.00"
        assert len(good.json()["splits"]) == 2

    def test_delete(self, client, checking, make_txn):
        txn = make_txn(checking["id"], "2026-08-02", "-5.00")
        client.delete(f"/api/transactions/{txn['id']}")
        assert client.get(f"/api/transactions/{txn['id']}").status_code == 404


class TestReimbursement:
    def test_inflow_to_expense_category_reduces_net_spend(
        self, client, checking, rent_category, make_txn
    ):
        """The girlfriend-Zelle-rent-share scenario: no 'other income' hack."""
        make_txn(checking["id"], "2026-08-01", "-2000.00", rent_category["id"], memo="August rent")
        make_txn(checking["id"], "2026-08-03", "800.00", rent_category["id"], memo="Zelle from GF")

        actuals = client.get(
            "/api/reports/category-actuals",
            params={"start": "2026-08-01", "end": "2026-08-31"},
        ).json()
        rent_row = next(row for row in actuals if row["category_id"] == rent_category["id"])
        assert rent_row["net"] == "-1200.00"


class TestTransfers:
    def test_paired_legs_without_category(self, client, checking, savings):
        response = client.post(
            "/api/transfers",
            json={
                "from_account_id": checking["id"],
                "to_account_id": savings["id"],
                "date": "2026-08-05",
                "amount": "300.00",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["out"]["total"] == "-300.00"
        assert body["in"]["total"] == "300.00"
        assert body["out"]["kind"] == "transfer"
        assert body["out"]["splits"][0]["category_id"] is None
        assert body["out"]["transfer_group_id"] == body["in"]["transfer_group_id"]
        assert client.get(f"/api/accounts/{checking['id']}").json()["balance"] == "700.00"
        assert client.get(f"/api/accounts/{savings['id']}").json()["balance"] == "300.00"

    def test_cross_currency_requires_to_amount(self, client, checking):
        cad = client.post(
            "/api/accounts", json={"name": "CAD wallet", "type": "cash", "currency_code": "CAD"}
        ).json()
        missing = client.post(
            "/api/transfers",
            json={
                "from_account_id": checking["id"],
                "to_account_id": cad["id"],
                "date": "2026-08-05",
                "amount": "100.00",
            },
        )
        assert missing.status_code == 422
        ok = client.post(
            "/api/transfers",
            json={
                "from_account_id": checking["id"],
                "to_account_id": cad["id"],
                "date": "2026-08-05",
                "amount": "100.00",
                "to_amount": "137.00",
            },
        )
        assert ok.json()["in"]["total"] == "137.00"

    def test_deleting_one_leg_deletes_both(self, client, checking, savings):
        body = client.post(
            "/api/transfers",
            json={
                "from_account_id": checking["id"],
                "to_account_id": savings["id"],
                "date": "2026-08-05",
                "amount": "300.00",
            },
        ).json()
        deleted = client.delete(f"/api/transactions/{body['out']['id']}").json()["deleted"]
        assert sorted(deleted) == sorted([body["out"]["id"], body["in"]["id"]])
        assert client.get(f"/api/accounts/{savings['id']}").json()["balance"] == "0.00"

    def test_transfer_excluded_from_cashflow(self, client, checking, savings):
        client.post(
            "/api/transfers",
            json={
                "from_account_id": checking["id"],
                "to_account_id": savings["id"],
                "date": "2026-08-05",
                "amount": "300.00",
            },
        )
        cashflow = client.get("/api/reports/cashflow", params={"months": 1, "end": "2026-08-31"}).json()
        assert cashflow[-1]["income"] == "0.00"
        assert cashflow[-1]["spending"] == "0.00"


class TestListFilters:
    def test_filter_by_amount_matches_absolute_total(self, client, checking, make_txn):
        make_txn(checking["id"], "2026-08-01", "-12.34")
        make_txn(checking["id"], "2026-08-02", "-99.99")
        hits = client.get("/api/transactions", params={"amount": "12.34"}).json()
        assert len(hits) == 1
        assert hits[0]["total"] == "-12.34"

    def test_filter_by_q_matches_payee_or_memo(self, client, checking, landlord, make_txn):
        make_txn(checking["id"], "2026-08-01", "-2000.00", payee_id=landlord["id"])
        make_txn(checking["id"], "2026-08-02", "-5.00", memo="coffee beans")
        assert len(client.get("/api/transactions", params={"q": "landlord"}).json()) == 1
        assert len(client.get("/api/transactions", params={"q": "beans"}).json()) == 1

    def test_uncategorized_filter(self, client, checking, groceries_category, make_txn):
        make_txn(checking["id"], "2026-08-01", "-1.00")
        make_txn(checking["id"], "2026-08-02", "-2.00", groceries_category["id"])
        hits = client.get("/api/transactions", params={"uncategorized": True}).json()
        assert len(hits) == 1
        assert hits[0]["total"] == "-1.00"


class TestPayeeAutofill:
    def test_payee_reports_default_and_last_transaction(
        self, client, checking, landlord, rent_category, make_txn
    ):
        make_txn(
            checking["id"], "2026-07-01", "-1950.00", rent_category["id"], payee_id=landlord["id"]
        )
        make_txn(
            checking["id"], "2026-08-01", "-2000.00", rent_category["id"], payee_id=landlord["id"]
        )
        payee = client.get(f"/api/payees/{landlord['id']}").json()
        assert payee["default_category_id"] == rent_category["id"]
        assert payee["last_amount"] == "-2000.00"
        assert payee["last_category_id"] == rent_category["id"]
