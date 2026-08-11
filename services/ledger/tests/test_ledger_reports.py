class TestCashflow:
    def test_income_vs_spending_by_month(
        self, client, checking, salary_category, groceries_category, make_txn
    ):
        make_txn(checking["id"], "2026-08-01", "5000.00", salary_category["id"])
        make_txn(checking["id"], "2026-08-10", "-300.00", groceries_category["id"])
        series = client.get(
            "/api/reports/cashflow", params={"months": 1, "end": "2026-08-31"}
        ).json()
        month = series[-1]
        assert month["month"] == "2026-08"
        assert month["income"] == "5000.00"
        assert month["spending"] == "300.00"
        assert month["net"] == "4700.00"

    def test_valuation_never_appears_in_cashflow(self, client, make_txn):
        """The -38k car write-down must NOT look like a monthly expense."""
        car = client.post(
            "/api/accounts",
            json={"name": "Car", "type": "asset", "opening_balance": "62000.00"},
        ).json()
        client.post(
            f"/api/accounts/{car['id']}/valuation",
            json={"date": "2026-08-01", "new_value": "24000.00"},
        )
        series = client.get(
            "/api/reports/cashflow", params={"months": 1, "end": "2026-08-31"}
        ).json()
        assert series[-1]["spending"] == "0.00"
        assert series[-1]["income"] == "0.00"

    def test_uncategorized_classified_by_sign(self, client, checking, make_txn):
        make_txn(checking["id"], "2026-08-01", "-50.00")
        series = client.get(
            "/api/reports/cashflow", params={"months": 1, "end": "2026-08-31"}
        ).json()
        assert series[-1]["spending"] == "50.00"


class TestNetWorth:
    def test_buckets_cash_vs_assets(self, client, checking, make_txn):
        client.post(
            "/api/accounts",
            json={"name": "Car", "type": "asset", "opening_balance": "24000.00"},
        )
        series = client.get("/api/reports/net-worth", params={"months": 1}).json()
        latest = series[-1]
        assert latest["cash"] == "1000.00"
        assert latest["assets"] == "24000.00"
        assert latest["total"] == "25000.00"

    def test_fx_conversion_with_manual_rate(self, client):
        client.post(
            "/api/accounts",
            json={
                "name": "CAD wallet",
                "type": "cash",
                "currency_code": "CAD",
                "opening_balance": "100.00",
            },
        )
        client.post(
            "/api/fx",
            json={"date": "2026-01-01", "from_code": "CAD", "to_code": "USD", "rate": "0.75"},
        )
        series = client.get("/api/reports/net-worth", params={"months": 1}).json()
        assert series[-1]["cash"] == "75.00"
        assert series[-1]["missing_rates"] == []

    def test_missing_rate_flagged_not_silent(self, client):
        client.post(
            "/api/accounts",
            json={
                "name": "TWD cash",
                "type": "cash",
                "currency_code": "TWD",
                "opening_balance": "1000.00",
            },
        )
        series = client.get("/api/reports/net-worth", params={"months": 1}).json()
        assert "TWD" in series[-1]["missing_rates"]


class TestFx:
    def test_inverse_rate_lookup(self, client):
        client.post(
            "/api/fx",
            json={"date": "2026-01-01", "from_code": "USD", "to_code": "CAD", "rate": "1.25"},
        )
        rate = client.get(
            "/api/fx/rate", params={"from_code": "CAD", "to_code": "USD", "on": "2026-06-01"}
        ).json()["rate"]
        assert rate.startswith("0.8")

    def test_upsert_same_day(self, client):
        client.post(
            "/api/fx",
            json={"date": "2026-01-01", "from_code": "CAD", "to_code": "USD", "rate": "0.70"},
        )
        client.post(
            "/api/fx",
            json={"date": "2026-01-01", "from_code": "CAD", "to_code": "USD", "rate": "0.71"},
        )
        rates = client.get("/api/fx").json()
        assert len(rates) == 1
        assert rates[0]["rate"] == "0.71"


class TestSeedDefaults:
    def test_seed_then_refuse_second_time(self, client):
        first = client.post("/api/seed-defaults")
        assert first.status_code == 200
        assert first.json()["created"] > 10
        assert client.post("/api/seed-defaults").status_code == 409


class TestExportImport:
    def test_round_trip(self, client, checking, rent_category, landlord, make_txn):
        make_txn(
            checking["id"], "2026-08-01", "-2000.00", rent_category["id"], payee_id=landlord["id"]
        )
        exported = client.get("/api/export").json()
        assert exported["service"] == "ledger"

        # Wipe by importing into the same instance, then verify contents.
        response = client.post("/api/import", json=exported)
        assert response.status_code == 200, response.text
        assert response.json()["imported"]["transactions"] == 1

        txns = client.get("/api/transactions").json()
        assert len(txns) == 1
        assert txns[0]["total"] == "-2000.00"
        assert txns[0]["payee_name"] == "Landlord LLC"
        assert client.get(f"/api/accounts/{checking['id']}").json()["balance"] == "-1000.00"

    def test_import_bad_shape_rejected(self, client):
        assert client.post("/api/import", json={"service": "ledger"}).status_code == 422
