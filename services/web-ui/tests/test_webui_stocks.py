from pathlib import Path

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
            "/import/profiles",
            data={
                "name": "ET",
                "kind": "stock",
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
            "/import/preview",
            data={"profile_id": "stock:1", "account_id": ""},
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
            "/import/preview",
            data={"profile_id": "stock:1", "account_id": ""},
            files={"file": ("trades.csv", content, "text/csv")},
            follow_redirects=True,
        )
        assert "trades.csv" in response.text
        assert "AAPL" in response.text
        summary = logged_in.post("/import/batches/stock/1/commit", follow_redirects=True)
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
        assert "Skipped" in logged_in.get("/monthly?month=2026-08").text


class TestBrokerageImportThroughTheUI:
    """The user-reported case, start to finish: a real broker activity export
    with a preamble, "--" placeholders, cash activity and a negative-quantity
    sale, imported from the same screen as a bank statement."""

    def seed(self, logged_in):
        logged_in.post(
            "/stocks/accounts",
            data={"name": "Brokerage", "type": "brokerage", "opening_cash": "0", "note": ""},
        )
        logged_in.post(
            "/import/profiles",
            data={
                "name": "Broker",
                "kind": "stock",
                "account_id": "1",
                "delimiter": ",",
                "has_header": "on",
                "skip_top_rows": "3",
                "date_column": "Activity/Trade Date",
                "date_format": "%m/%d/%Y",
                "action_column": "Activity Type",
                "description_column": "Description",
                "symbol_column": "Symbol",
                "quantity_column": "Quantity #",
                "price_column": "Price $",
                "amount_column": "Amount $",
                "fee_column": "Commission",
                "map_from_0": "Bought",
                "map_to_0": "buy",
                "map_from_1": "Sold",
                "map_to_1": "sell",
                "map_from_2": "Dividend",
                "map_to_2": "dividend",
                "map_from_3": "Online Transfer",
                "map_to_3": "deposit",
                "map_from_4": "Service Fee",
                "map_to_4": "fee",
                "action_map_json": "",
            },
        )

    def test_sample_export_imports_from_the_shared_screen(
        self, logged_in, stocks_api, broker_activity_csv
    ):
        self.seed(logged_in)
        review = logged_in.post(
            "/import/preview",
            data={"profile_id": "stock:1", "account_id": ""},
            files={"file": ("activity.csv", broker_activity_csv, "text/csv")},
            follow_redirects=True,
        )
        assert review.status_code == 200
        assert "activity.csv" in review.text
        # The sale of a position bought before this window is held back, named,
        # and explained rather than blowing up the import.
        assert "No shares held" in review.text
        assert "EXL" in review.text

        summary = logged_in.post("/import/batches/stock/1/commit", follow_redirects=True)
        assert "Imported 9 stock transaction" in summary.text
        account = stocks_api.get("/api/accounts/1").json()
        # 1000 deposit - 2.00 fee + 12.00 dividends - 245.00 purchases
        assert account["cash"] == "765.00"
        assert {h["symbol"] for h in account["holdings"]} == {"EXD", "EXL", "EXM", "EXB"}

    def test_reuploading_the_same_export_imports_nothing_twice(
        self, logged_in, stocks_api, broker_activity_csv
    ):
        self.seed(logged_in)
        logged_in.post(
            "/import/preview",
            data={"profile_id": "stock:1", "account_id": ""},
            files={"file": ("activity.csv", broker_activity_csv, "text/csv")},
        )
        logged_in.post("/import/batches/stock/1/commit")
        before = stocks_api.get("/api/accounts/1").json()["cash"]

        logged_in.post(
            "/import/preview",
            data={"profile_id": "stock:1", "account_id": ""},
            files={"file": ("activity.csv", broker_activity_csv, "text/csv")},
        )
        logged_in.post("/import/batches/stock/2/commit")
        assert stocks_api.get("/api/accounts/1").json()["cash"] == before


class TestProfileFormRendering:
    def test_brokerage_form_offers_the_stock_fields(self, logged_in):
        page = logged_in.get("/import/profiles?kind=stock")
        assert 'name="action_column"' in page.text
        assert 'name="symbol_column"' in page.text
        assert 'name="description_column"' in page.text  # was missing before
        assert 'name="skip_top_rows"' in page.text
        assert 'name="map_from_0"' in page.text
        assert 'name="amount_mode"' not in page.text  # bank-only field

    def test_bank_form_is_the_default_and_hides_stock_fields(self, logged_in):
        page = logged_in.get("/import/profiles")
        assert 'name="amount_mode"' in page.text
        assert 'name="action_column"' not in page.text

    def test_saved_brokerage_profile_round_trips_through_edit(self, logged_in):
        logged_in.post(
            "/stocks/accounts",
            data={"name": "E*Trade", "type": "brokerage", "opening_cash": "0", "note": ""},
        )
        logged_in.post(
            "/import/profiles",
            data={
                "name": "ET",
                "kind": "stock",
                "account_id": "1",
                "delimiter": ",",
                "has_header": "on",
                "skip_top_rows": "3",
                "date_column": "Activity/Trade Date",
                "date_format": "%m/%d/%Y",
                "action_column": "Activity Type",
                "description_column": "Description",
                "symbol_column": "Symbol",
                "map_from_0": "Bought",
                "map_to_0": "buy",
                "action_map_json": '{"Sold": "sell"}',
            },
        )
        page = logged_in.get("/import/profiles?edit=stock:1")
        assert 'value="Activity/Trade Date"' in page.text
        assert 'value="Activity Type"' in page.text
        assert 'value="Description"' in page.text
        assert 'value="3"' in page.text
        # Both the row-entered and the JSON-entered mappings come back editable.
        assert 'value="Bought"' in page.text
        assert 'value="Sold"' in page.text

    def test_profile_can_be_deleted(self, logged_in):
        logged_in.post(
            "/stocks/accounts",
            data={"name": "E*Trade", "type": "brokerage", "opening_cash": "0", "note": ""},
        )
        logged_in.post(
            "/import/profiles",
            data={
                "name": "Throwaway",
                "kind": "stock",
                "account_id": "1",
                "date_column": "date",
                "action_column": "type",
                "map_from_0": "Bought",
                "map_to_0": "buy",
                "action_map_json": "",
            },
        )
        assert "Throwaway" in logged_in.get("/import/profiles").text
        logged_in.post("/import/profiles/stock/1/delete")
        assert "Throwaway" not in logged_in.get("/import/profiles").text


class TestBlankFieldsNeverShowRawJson:
    """Submitting an empty form used to dump FastAPI's validation JSON at the
    user. Every form answers with a redirect and a readable message instead."""

    @pytest.fixture()
    def account(self, logged_in):
        logged_in.post(
            "/stocks/accounts",
            data={"name": "Brokerage", "type": "brokerage", "opening_cash": "0", "note": ""},
        )

    def submit(self, logged_in, url, data):
        response = logged_in.post(url, data=data, follow_redirects=False)
        assert "json" not in response.headers.get("content-type", ""), response.text[:200]
        assert response.status_code == 303, response.text[:200]
        return response.headers["location"]

    def test_blank_account_form(self, logged_in, stocks_api):
        location = self.submit(
            logged_in,
            "/stocks/accounts",
            {"name": "", "type": "brokerage", "opening_cash": "", "note": ""},
        )
        assert "err=" in location
        assert stocks_api.get("/api/accounts").json() == []

    def test_blank_manual_price_form(self, logged_in):
        location = self.submit(
            logged_in, "/stocks/prices/manual", {"symbol": "", "date": "", "close": ""}
        )
        assert "symbol" in location

    def test_blank_transaction_form(self, logged_in, account):
        location = self.submit(
            logged_in,
            "/stocks/accounts/1/transactions",
            {"type": "buy", "date": "", "symbol": "", "quantity": "",
             "price": "", "amount": "", "fees": "", "note": ""},
        )
        assert "date" in location

    def test_blank_grant_form(self, logged_in, account):
        location = self.submit(
            logged_in, "/stocks/accounts/1/grants", {"symbol": "", "grant_date": "", "note": ""}
        )
        assert "err=" in location

    def test_chart_without_a_symbol(self, logged_in):
        response = logged_in.get("/stocks/chart?symbol=", follow_redirects=False)
        assert response.status_code == 303
        assert "json" not in response.headers.get("content-type", "")

    def test_chart_with_an_unreadable_range_falls_back(self, logged_in, stocks_api):
        stocks_api.post("/api/prices/backfill/AAPL")
        page = logged_in.get("/stocks/chart?symbol=AAPL&months=")
        assert page.status_code == 200
        assert "AAPL price history" in page.text

    def test_a_missing_field_entirely_still_renders_html(self, logged_in):
        """The catch-all handler, not per-field checks: even a form posted
        with fields absent gets an HTML answer."""
        response = logged_in.post("/stocks/prices/manual", data={})
        assert "json" not in response.headers.get("content-type", "")
        assert response.status_code in (200, 303)
