from pathlib import Path

import pytest

SAMPLES = Path(__file__).resolve().parents[3] / "samples"

PROFILE_CONFIG = {
    "date_column": "date",
    "date_format": "%m/%d/%Y",
    "action_column": "type",
    "symbol_column": "symbol",
    "quantity_column": "qty",
    "price_column": "price",
    "amount_column": "amount",
    "action_map": {"Bought": "buy", "YOU SOLD": "sell", "WIRE IN": "deposit", "DIV": "dividend"},
}


@pytest.fixture()
def profile(client, brokerage):
    return client.post(
        "/api/import/profiles",
        json={"name": "E*Trade", "account_id": brokerage["id"], "config": PROFILE_CONFIG},
    ).json()


def preview(client, profile, content):
    return client.post(
        "/api/import/preview",
        json={"profile_id": profile["id"], "filename": "trades.csv", "content": content},
    )


GOOD_CSV = (
    "Date,Type,Symbol,Qty,Price,Amount\n"
    "07/01/2026,WIRE IN,,,,2000.00\n"
    "07/02/2026,Bought,AAPL,10,150.00,1500.00\n"
    "07/20/2026,DIV,AAPL,,,5.00\n"
)


class TestProfileValidation:
    def test_action_map_required(self, client, brokerage):
        response = client.post(
            "/api/import/profiles",
            json={
                "name": "Bad",
                "account_id": brokerage["id"],
                "config": {"date_column": "date", "action_column": "type"},
            },
        )
        assert response.status_code == 422
        assert "action_map" in response.json()["detail"]


class TestPreview:
    def test_unmapped_action_aborts_whole_file(self, client, profile):
        content = GOOD_CSV + "07/25/2026,Reinvest,AAPL,1,150.00,150.00\n"
        response = preview(client, profile, content)
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert "nothing was imported" in detail["message"]
        assert any("Reinvest" in e["message"] for e in detail["errors"])
        assert client.get("/api/transactions").json() == []
        assert client.get("/api/import/batches").json() == []

    def test_valid_file_builds_batch(self, client, profile):
        batch = preview(client, profile, GOOD_CSV).json()
        assert batch["status"] == "review"
        assert batch["total_rows"] == 3
        assert [r["action"] for r in batch["rows"]] == ["deposit", "buy", "dividend"]


class TestCommit:
    def test_commit_applies_portfolio_effects(self, client, profile, brokerage):
        batch = preview(client, profile, GOOD_CSV).json()
        summary = client.post(f"/api/import/batches/{batch['id']}/commit").json()
        assert summary["created"] == 3
        assert summary["symbols"] == ["AAPL"]
        account = client.get(f"/api/accounts/{brokerage['id']}").json()
        # 10000 opening + 2000 wire - 1500 buy + 5 dividend
        assert account["cash"] == "10505.00"
        assert account["holdings"][0]["quantity"] == "10"

    def test_reupload_is_fully_deduplicated(self, client, profile):
        first = preview(client, profile, GOOD_CSV).json()
        client.post(f"/api/import/batches/{first['id']}/commit")
        second = preview(client, profile, GOOD_CSV).json()
        assert second["row_counts"] == {"duplicate": 3}
        summary = client.post(f"/api/import/batches/{second['id']}/commit").json()
        assert summary["created"] == 0

BROKER_CONFIG = {
    # Three visible junk lines sit above the header; the blank lines between
    # them are not counted.
    "skip_top_rows": 3,
    "date_column": "Activity/Trade Date",
    "date_format": "%m/%d/%Y",
    "action_column": "Activity Type",
    "description_column": "Description",
    "symbol_column": "Symbol",
    "quantity_column": "Quantity #",
    "price_column": "Price $",
    "amount_column": "Amount $",
    "fee_column": "Commission",
    "action_map": {
        "Online Transfer": "deposit",
        "Service Fee": "fee",
        "Dividend": "dividend",
        "Bought": "buy",
        "Sold": "sell",
    },
}


@pytest.fixture()
def broker_profile(client, brokerage):
    return client.post(
        "/api/import/profiles",
        json={"name": "Broker", "account_id": brokerage["id"], "config": BROKER_CONFIG},
    ).json()


class TestRealBrokerageExport:
    """The shape brokers actually ship: a multi-line preamble, "--" where a
    column doesn't apply, negative quantities on sales, and cash activity mixed
    in with trades. Figures are invented (see the root conftest)."""

    def test_preamble_and_placeholders_parse(self, client, broker_profile, broker_activity_csv):
        batch = preview(client, broker_profile, broker_activity_csv).json()
        assert batch["total_rows"] == 10
        rows = {r["line_no"]: r for r in batch["rows"]}
        # Cash rows carry "--" in the Symbol column; that is not a ticker.
        assert rows[8]["action"] == "deposit"
        assert rows[8]["symbol"] == ""
        assert rows[9]["action"] == "fee"
        assert rows[9]["symbol"] == ""
        # A dividend keeps the security it came from.
        assert (rows[10]["action"], rows[10]["symbol"]) == ("dividend", "EXB")

    def test_sale_quantity_is_stored_as_a_magnitude(
        self, client, broker_profile, broker_activity_csv
    ):
        batch = preview(client, broker_profile, broker_activity_csv).json()
        sale = next(r for r in batch["rows"] if r["action"] == "sell")
        assert sale["symbol"] == "EXI"
        assert sale["quantity"] == "1.5"

    def test_sale_without_a_prior_lot_is_flagged_not_fatal(
        self, client, broker_profile, broker_activity_csv
    ):
        """EXI was bought before this statement's window, so FIFO has no lot.
        The row is held back instead of failing the whole import."""
        batch = preview(client, broker_profile, broker_activity_csv).json()
        sale = next(r for r in batch["rows"] if r["action"] == "sell")
        assert sale["status"] == "no_lots"
        assert sale["include"] is False

        summary = client.post(f"/api/import/batches/{batch['id']}/commit").json()
        assert summary["created"] == 9
        assert summary["skipped"] == 1

    def test_cash_and_trades_land_with_the_right_signs(
        self, client, broker_profile, brokerage, broker_activity_csv
    ):
        batch = preview(client, broker_profile, broker_activity_csv).json()
        client.post(f"/api/import/batches/{batch['id']}/commit")
        account = client.get(f"/api/accounts/{brokerage['id']}").json()
        # 10000 opening + 1000 deposit - 2.00 fee + 12.00 dividends
        # - 245.00 of purchases (5 + 120 + 20 + 100)
        assert account["cash"] == "10765.00"
        holdings = {h["symbol"]: h["quantity"] for h in account["holdings"]}
        assert holdings == {"EXD": "0.1", "EXL": "2", "EXM": "0.5", "EXB": "1"}

    def test_sale_imports_once_the_opening_position_exists(
        self, client, broker_profile, brokerage, broker_activity_csv
    ):
        client.post(
            "/api/transactions",
            json={
                "account_id": brokerage["id"],
                "type": "buy",
                "date": "2024-06-18",
                "symbol": "EXI",
                "quantity": "2",
                "price": "48.00",
            },
        )
        batch = preview(client, broker_profile, broker_activity_csv).json()
        sale = next(r for r in batch["rows"] if r["action"] == "sell")
        assert sale["status"] == "ready"
        summary = client.post(f"/api/import/batches/{batch['id']}/commit").json()
        assert summary["created"] == 10

    def test_reimporting_the_same_export_changes_nothing(
        self, client, broker_profile, broker_activity_csv
    ):
        first = preview(client, broker_profile, broker_activity_csv).json()
        client.post(f"/api/import/batches/{first['id']}/commit")
        second = preview(client, broker_profile, broker_activity_csv).json()
        statuses = [r["status"] for r in second["rows"]]
        assert statuses.count("duplicate") == 9
        summary = client.post(f"/api/import/batches/{second['id']}/commit").json()
        assert summary["created"] == 0


class TestSameDayRepeats:
    def test_identical_trades_in_one_file_both_import(self, client, profile):
        """Two fills of the same size at the same price on one day are two real
        trades — the file is never deduplicated against itself."""
        content = (
            "Date,Type,Symbol,Qty,Price,Amount\n"
            "07/02/2026,Bought,AAPL,10,150.00,1500.00\n"
            "07/02/2026,Bought,AAPL,10,150.00,1500.00\n"
        )
        batch = preview(client, profile, content).json()
        assert [r["status"] for r in batch["rows"]] == ["ready", "ready"]
        summary = client.post(f"/api/import/batches/{batch['id']}/commit").json()
        assert summary["created"] == 2

    def test_reimport_matches_repeats_one_for_one(self, client, profile):
        content = (
            "Date,Type,Symbol,Qty,Price,Amount\n"
            "07/02/2026,Bought,AAPL,10,150.00,1500.00\n"
            "07/02/2026,Bought,AAPL,10,150.00,1500.00\n"
        )
        first = preview(client, profile, content).json()
        client.post(f"/api/import/batches/{first['id']}/commit")
        second = preview(client, profile, content).json()
        assert second["row_counts"] == {"duplicate": 2}


class TestBatchesListing:
    def test_batches_listed_for_monthly_page(self, client, profile):
        batch = preview(client, profile, GOOD_CSV).json()
        client.post(f"/api/import/batches/{batch['id']}/commit")
        from datetime import date

        month = date.today().strftime("%Y-%m")
        listed = client.get("/api/import/batches", params={"month": month}).json()
        assert len(listed) == 1
        assert listed[0]["status"] == "committed"
