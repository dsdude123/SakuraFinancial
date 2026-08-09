class TestAccounts:
    def test_create_and_balance_includes_opening(self, client, checking):
        assert checking["balance"] == "1000.00"

    def test_bad_type_rejected(self, client):
        response = client.post("/api/accounts", json={"name": "X", "type": "offshore"})
        assert response.status_code == 422

    def test_unknown_currency_rejected(self, client):
        response = client.post(
            "/api/accounts", json={"name": "X", "type": "cash", "currency_code": "EUR"}
        )
        assert response.status_code == 422
        assert "add it first" in response.json()["detail"]

    def test_duplicate_name_conflict(self, client, checking):
        response = client.post("/api/accounts", json={"name": "Checking", "type": "cash"})
        assert response.status_code == 409

    def test_foreign_currency_cash_account(self, client):
        response = client.post(
            "/api/accounts", json={"name": "CAD wallet", "type": "cash", "currency_code": "CAD"}
        )
        assert response.status_code == 200
        assert response.json()["currency_code"] == "CAD"

    def test_balance_moves_with_transactions(self, client, checking, make_txn, groceries_category):
        make_txn(checking["id"], "2026-08-01", "-250.00", groceries_category["id"])
        assert client.get(f"/api/accounts/{checking['id']}").json()["balance"] == "750.00"

    def test_delete_with_transactions_conflicts(self, client, checking, make_txn):
        make_txn(checking["id"], "2026-08-01", "-1.00")
        response = client.delete(f"/api/accounts/{checking['id']}")
        assert response.status_code == 409
        assert "deactivate" in response.json()["detail"]

    def test_deactivate_hides_from_default_list(self, client, checking):
        client.put(f"/api/accounts/{checking['id']}", json={"active": False})
        names = [a["name"] for a in client.get("/api/accounts").json()]
        assert "Checking" not in names
        names_all = [
            a["name"] for a in client.get("/api/accounts", params={"include_inactive": True}).json()
        ]
        assert "Checking" in names_all


class TestAssetValuation:
    def test_car_depreciation_flow(self, client, make_txn):
        car = client.post(
            "/api/accounts",
            json={"name": "Car", "type": "asset", "opening_balance": "62000.00"},
        ).json()

        response = client.post(
            f"/api/accounts/{car['id']}/valuation",
            json={"date": "2026-08-01", "new_value": "24000.00"},
        )
        assert response.status_code == 200
        txn = response.json()
        assert txn["kind"] == "valuation"
        assert txn["total"] == "-38000.00"
        assert txn["splits"][0]["category_id"] is None
        assert client.get(f"/api/accounts/{car['id']}").json()["balance"] == "24000.00"

    def test_valuation_rejected_on_cash_account(self, client, checking):
        response = client.post(
            f"/api/accounts/{checking['id']}/valuation",
            json={"date": "2026-08-01", "new_value": "5.00"},
        )
        assert response.status_code == 422

    def test_unchanged_value_rejected(self, client):
        car = client.post(
            "/api/accounts", json={"name": "Car", "type": "asset", "opening_balance": "10.00"}
        ).json()
        response = client.post(
            f"/api/accounts/{car['id']}/valuation",
            json={"date": "2026-08-01", "new_value": "10.00"},
        )
        assert response.status_code == 422
