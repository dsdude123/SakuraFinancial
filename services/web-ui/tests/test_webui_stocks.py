import pytest
from fastapi.testclient import TestClient

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@pytest.fixture()
def stocks_api(stack):
    return TestClient(stack["stocks"])


class TestStocksPages:
    def test_create_account_and_overview(self, logged_in):
        response = logged_in.post(
            "/stocks/accounts",
            data={"name": "Fidelity", "type": "brokerage", "opening_cash": "10000.00", "note": ""},
        )
        assert response.status_code == 303
        page = logged_in.get("/stocks")
        assert "Fidelity" in page.text
        assert "10,000.00" in page.text

    def test_record_buy_and_see_holding(self, logged_in):
        logged_in.post(
            "/stocks/accounts",
            data={"name": "Fidelity", "type": "brokerage", "opening_cash": "10000.00", "note": ""},
        )
        logged_in.post(
            "/stocks/accounts/1/transactions",
            data={
                "type": "buy",
                "date": "2026-08-01",
                "symbol": "aapl",
                "quantity": "10",
                "price": "150.00",
                "amount": "",
                "fees": "",
                "note": "",
            },
        )
        page = logged_in.get("/stocks/accounts/1")
        assert "AAPL" in page.text
        assert "8,500.00" in page.text  # cash after buy

    def test_refresh_prices_via_ui(self, logged_in, stocks_api):
        logged_in.post(
            "/stocks/accounts",
            data={"name": "Fidelity", "type": "brokerage", "opening_cash": "0", "note": ""},
        )
        stocks_api.post("/api/prices/backfill/AAPL")
        response = logged_in.post("/stocks/prices/refresh", follow_redirects=True)
        assert "Updated 1 symbol" in response.text

    def test_stock_chart_page_and_png(self, logged_in, stocks_api):
        stocks_api.post("/api/prices/backfill/AAPL")
        page = logged_in.get("/stocks/chart?symbol=AAPL")
        assert "AAPL price history" in page.text
        chart = logged_in.get("/charts/stock.png?symbol=AAPL")
        assert chart.content.startswith(PNG_SIGNATURE)

    def test_managed_account_hides_trading_and_analysis(self, logged_in):
        logged_in.post(
            "/stocks/accounts",
            data={"name": "Smart Portfolio", "type": "managed", "opening_cash": "5000", "note": ""},
        )
        page = logged_in.get("/stocks/accounts/1")
        assert "managed portfolio" in page.text
        assert "Analyze this account" not in page.text
        assert 'value="buy"' not in page.text

    def test_rsu_grant_and_release_flow(self, logged_in, stocks_api):
        logged_in.post(
            "/stocks/accounts",
            data={"name": "Work RSUs", "type": "rsu", "opening_cash": "0", "note": ""},
        )
        stocks_api.post(
            "/api/prices", json={"symbol": "MSFT", "date": "2026-08-01", "close": "400.00"}
        )
        logged_in.post(
            "/stocks/accounts/1/grants",
            data={
                "symbol": "MSFT",
                "grant_date": "2025-08-01",
                "note": "",
                "vest_date_0": "2026-08-01",
                "vest_shares_0": "25",
                "vest_date_1": "2027-08-01",
                "vest_shares_1": "25",
            },
        )
        page = logged_in.get("/stocks/accounts/1")
        assert "vested — release below" in page.text

        vest_id = stocks_api.get("/api/rsu/grants").json()[0]["vesting"][0]["id"]
        logged_in.post(f"/stocks/vests/{vest_id}/release", data={"account_id": "1", "price": ""})
        page = logged_in.get("/stocks/accounts/1")
        assert "released" in page.text
        assert "10,000.00" in page.text  # 25 shares * 400

    def test_analysis_without_llm(self, logged_in, stocks_api):
        logged_in.post(
            "/stocks/accounts",
            data={"name": "Fidelity", "type": "brokerage", "opening_cash": "10000", "note": ""},
        )
        logged_in.post(
            "/stocks/accounts/1/transactions",
            data={
                "type": "buy", "date": "2026-08-01", "symbol": "AAPL",
                "quantity": "10", "price": "150.00", "amount": "", "fees": "", "note": "",
            },
        )
        response = logged_in.post("/stocks/accounts/1/analyze", follow_redirects=True)
        assert "No AI guidance" in response.text
        assert "AAPL" in response.text


class TestStockImportUI:
    def seed(self, logged_in):
        logged_in.post(
            "/stocks/accounts",
            data={"name": "E*Trade", "type": "brokerage", "opening_cash": "0", "note": ""},
        )
        logged_in.post(
            "/stocks/import/profiles",
            data={
                "name": "ET",
                "account_id": "1",
                "delimiter": ",",
                "has_header": "on",
                "skip_top_rows": "0",
                "date_column": "date",
                "date_format": "%m/%d/%Y",
                "action_column": "type",
                "symbol_column": "symbol",
                "quantity_column": "qty",
                "price_column": "price",
                "amount_column": "amount",
                "fee_column": "",
                "description_column": "",
                "map_from_0": "Bought",
                "map_to_0": "buy",
                "map_from_1": "WIRE IN",
                "map_to_1": "deposit",
                "action_map_json": "",
            },
        )

    def test_unmapped_action_shows_error_report(self, logged_in):
        self.seed(logged_in)
        content = "Date,Type,Symbol,Qty,Price,Amount\n08/01/2026,Reinvest,AAPL,1,150.00,150.00\n"
        response = logged_in.post(
            "/stocks/import/preview",
            data={"profile_id": "1", "account_id": ""},
            files={"file": ("trades.csv", content, "text/csv")},
        )
        assert "didn't pass validation" in response.text
        assert "Reinvest" in response.text

    def test_good_file_review_and_commit(self, logged_in, stocks_api):
        self.seed(logged_in)
        content = (
            "Date,Type,Symbol,Qty,Price,Amount\n"
            "07/01/2026,WIRE IN,,,,2000.00\n"
            "07/02/2026,Bought,AAPL,10,150.00,1500.00\n"
        )
        response = logged_in.post(
            "/stocks/import/preview",
            data={"profile_id": "1", "account_id": ""},
            files={"file": ("trades.csv", content, "text/csv")},
            follow_redirects=True,
        )
        assert "Review: trades.csv" in response.text
        summary = logged_in.post("/stocks/import/batches/1/commit", follow_redirects=True)
        assert "Imported 2 stock transaction" in summary.text
        account = stocks_api.get("/api/accounts/1").json()
        assert account["cash"] == "500.00"
        assert account["holdings"][0]["quantity"] == "10"


class TestMonthlyIntegration:
    def test_stock_accounts_appear_on_monthly_page(self, logged_in):
        logged_in.post(
            "/stocks/accounts",
            data={"name": "Fidelity", "type": "brokerage", "opening_cash": "0", "note": ""},
        )
        page = logged_in.get("/monthly?month=2026-08")
        assert "Fidelity" in page.text
        # skip a stock account for the month
        logged_in.post("/monthly/2026-08/skip", data={"service": "stocks", "account_id": "1"})
        assert "skipped" in logged_in.get("/monthly?month=2026-08").text
