import pytest

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

    def test_batches_listed_for_monthly_page(self, client, profile):
        batch = preview(client, profile, GOOD_CSV).json()
        client.post(f"/api/import/batches/{batch['id']}/commit")
        from datetime import date

        month = date.today().strftime("%Y-%m")
        listed = client.get("/api/import/batches", params={"month": month}).json()
        assert len(listed) == 1
        assert listed[0]["status"] == "committed"
