class TestAccounts:
    def test_types_enforced(self, client):
        assert (
            client.post("/api/accounts", json={"name": "X", "type": "offshore"}).status_code == 422
        )

    def test_cash_starts_at_opening(self, client, brokerage):
        assert brokerage["cash"] == "10000.00"
        assert brokerage["total"] == "10000.00"


class TestBuySell:
    def test_buy_creates_lot_and_spends_cash(self, client, brokerage, buy_aapl):
        buy_aapl()
        account = client.get(f"/api/accounts/{brokerage['id']}").json()
        assert account["cash"] == "8500.00"  # 10000 - 10*150
        holding = account["holdings"][0]
        assert holding["symbol"] == "AAPL"
        assert holding["quantity"] == "10"
        assert holding["cost_basis"] == "1500.00"

    def test_sell_fifo_realized_gain(self, client, brokerage, buy_aapl):
        buy_aapl(quantity="10", price="100.00", date="2026-07-01")
        buy_aapl(quantity="10", price="200.00", date="2026-07-15")
        response = client.post(
            "/api/transactions",
            json={
                "account_id": brokerage["id"],
                "type": "sell",
                "date": "2026-08-01",
                "symbol": "AAPL",
                "quantity": "15",
                "price": "250.00",
            },
        ).json()
        # proceeds 3750; basis removed = 10*100 + 5*200 = 2000 (FIFO)
        assert response["realized_gain"] == "1750.00"
        account = client.get(f"/api/accounts/{brokerage['id']}").json()
        assert account["holdings"][0]["quantity"] == "5"
        assert account["holdings"][0]["cost_basis"] == "1000.00"

    def test_cannot_sell_more_than_held(self, client, brokerage, buy_aapl):
        buy_aapl(quantity="5")
        response = client.post(
            "/api/transactions",
            json={
                "account_id": brokerage["id"],
                "type": "sell",
                "date": "2026-08-02",
                "symbol": "AAPL",
                "quantity": "6",
                "price": "150.00",
            },
        )
        assert response.status_code == 422
        assert "only 5" in response.json()["detail"]

    def test_deposit_withdraw_dividend(self, client, managed):
        for body, expected_cash in [
            ({"type": "deposit", "amount": "1000.00"}, "6000.00"),
            ({"type": "withdraw", "amount": "500.00"}, "5500.00"),
            ({"type": "dividend", "amount": "25.00", "symbol": "VTI"}, "5525.00"),
        ]:
            response = client.post(
                "/api/transactions",
                json={"account_id": managed["id"], "date": "2026-08-01", **body},
            )
            assert response.status_code == 200, response.text
            assert client.get(f"/api/accounts/{managed['id']}").json()["cash"] == expected_cash


class TestPricesAndValuation:
    def test_manual_price_and_market_value(self, client, brokerage, buy_aapl):
        buy_aapl()
        client.post("/api/prices", json={"symbol": "AAPL", "date": "2026-08-08", "close": "160.00"})
        account = client.get(f"/api/accounts/{brokerage['id']}").json()
        holding = account["holdings"][0]
        assert holding["last_price"] == "160.00"
        assert holding["market_value"] == "1600.00"
        assert holding["unrealized_gain"] == "100.00"
        assert account["total"] == "10100.00"  # 8500 cash + 1600 market

    def test_refresh_pulls_latest_close_from_yahoo(self, client, brokerage, buy_aapl):
        buy_aapl()
        result = client.post("/api/prices/refresh").json()
        assert result["updated"] == ["AAPL"]
        history = client.get("/api/prices/AAPL").json()
        assert history[-1]["close"] == "152.50"
        assert history[-1]["source"] == "yahoo"

    def test_manual_price_survives_yahoo_refresh(self, client, brokerage, buy_aapl):
        buy_aapl()
        client.post("/api/prices", json={"symbol": "AAPL", "date": "2026-08-07", "close": "999.00"})
        client.post("/api/prices/refresh")
        history = {row["date"]: row for row in client.get("/api/prices/AAPL").json()}
        assert history["2026-08-07"]["close"] == "999.00"
        assert history["2026-08-07"]["source"] == "manual"

    def test_backfill_endpoint(self, client):
        result = client.post("/api/prices/backfill/AAPL").json()
        assert result["prices_loaded"] == 2

    def test_valuation_series_months(self, client, brokerage, buy_aapl):
        """The series is the last N months counted back from today, so the test
        data has to be dated relative to today too — pinned dates passed until
        the clock moved past them, then this failed on its own."""
        import datetime as dt

        last_month_end = dt.date.today().replace(day=1) - dt.timedelta(days=1)
        buy_aapl(date=last_month_end.replace(day=1).isoformat())
        client.post(
            "/api/prices",
            json={"symbol": "AAPL", "date": last_month_end.isoformat(), "close": "155.00"},
        )
        series = client.get("/api/valuation/series", params={"months": 3}).json()
        assert len(series) == 3
        # Last month: cash 8500 + 10*155
        month = last_month_end.strftime("%Y-%m")
        row = next(r for r in series if r["month"] == month)
        assert row["total"] == "10050.00"


class TestRsu:
    def make_grant(self, client, rsu_account):
        return client.post(
            "/api/rsu/grants",
            json={
                "account_id": rsu_account["id"],
                "symbol": "MSFT",
                "grant_date": "2025-08-01",
                "vesting": [
                    {"vest_date": "2026-08-01", "shares": "25"},
                    {"vest_date": "2027-08-01", "shares": "25"},
                ],
            },
        ).json()

    def test_grant_requires_rsu_account(self, client, brokerage):
        response = client.post(
            "/api/rsu/grants",
            json={
                "account_id": brokerage["id"],
                "symbol": "MSFT",
                "grant_date": "2025-08-01",
                "vesting": [{"vest_date": "2026-08-01", "shares": "25"}],
            },
        )
        assert response.status_code == 422

    def test_vesting_schedule_flags_due_events(self, client, rsu_account):
        self.make_grant(client, rsu_account)
        grants = client.get("/api/rsu/grants").json()
        vesting = grants[0]["vesting"]
        assert vesting[0]["due"] is True  # 2026-08-01 has passed
        assert vesting[1]["due"] is False

    def test_release_creates_lot_at_market_value(self, client, rsu_account):
        grant = self.make_grant(client, rsu_account)
        del grant
        client.post("/api/prices", json={"symbol": "MSFT", "date": "2026-08-01", "close": "400.00"})
        vest_id = client.get("/api/rsu/grants").json()[0]["vesting"][0]["id"]
        result = client.post(f"/api/rsu/vests/{vest_id}/release", json={})
        assert result.status_code == 200, result.text

        account = client.get(f"/api/accounts/{rsu_account['id']}").json()
        holding = account["holdings"][0]
        assert holding["quantity"] == "25"
        assert holding["cost_basis"] == "10000.00"  # 25 * 400 vest-day value
        # releasing twice is refused
        assert client.post(f"/api/rsu/vests/{vest_id}/release", json={}).status_code == 422


class TestManagedAccounts:
    def test_analysis_refused_for_managed(self, client, managed):
        response = client.post("/api/analyze", json={"account_id": managed["id"]})
        assert response.status_code == 422
        assert "managed" in response.json()["detail"]


class TestAnalysis:
    def test_analysis_without_llm_returns_fundamentals_only(self, client, brokerage, buy_aapl):
        buy_aapl()
        client.post("/api/prices/refresh")
        result = client.post("/api/analyze", json={"account_id": brokerage["id"]}).json()
        assert result["ai_text"] is None
        assert result["fundamentals"]["holdings"][0]["symbol"] == "AAPL"
        assert result["fundamentals"]["valuation"]["cash"] == "8500.00"
        # stored and listable
        assert len(client.get("/api/analyses").json()) == 1

    def test_analysis_with_llm_includes_trading_philosophy(
        self, client, brokerage, buy_aapl, settings, monkeypatch
    ):
        import sakura_common.llm

        settings.values.update(
            {
                "llm.provider": "anthropic",
                "llm.api_key": "sk-test",
                "llm.model": "claude-test",
                "stocks.ai_instructions": "I hold long term and care about tax implications.",
            }
        )
        captured = {}

        def fake_complete(self, prompt, system=None, **kwargs):
            captured["prompt"] = prompt
            return "Steady as she goes."

        monkeypatch.setattr(sakura_common.llm.LLMClient, "complete", fake_complete)
        buy_aapl()
        result = client.post("/api/analyze", json={"account_id": brokerage["id"]}).json()
        assert result["ai_text"] == "Steady as she goes."
        assert "tax implications" in captured["prompt"]
        assert "AAPL" in captured["prompt"]


class TestExportImport:
    def test_round_trip(self, client, brokerage, buy_aapl):
        buy_aapl()
        client.post("/api/prices", json={"symbol": "AAPL", "date": "2026-08-08", "close": "160.00"})
        exported = client.get("/api/export").json()
        assert exported["service"] == "stocks"
        response = client.post("/api/import", json=exported)
        assert response.status_code == 200, response.text
        account = client.get(f"/api/accounts/{brokerage['id']}").json()
        assert account["cash"] == "8500.00"
        assert account["holdings"][0]["market_value"] == "1600.00"
