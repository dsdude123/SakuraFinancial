from datetime import date
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from sakura_common.yahoo import YahooClient

from stocks_service.main import create_app


class FakeSettings:
    def __init__(self):
        self.values: dict[str, str] = {}

    def get(self, key, default=None):
        return self.values.get(key, default)


def make_yahoo(prices: dict[str, list[tuple[str, float]]]):
    """Yahoo stub via MockTransport: `prices` maps symbol -> [(iso_date, close)]."""
    from datetime import datetime, timezone

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for symbol, rows in prices.items():
            if f"/chart/{symbol}" in url:
                timestamps = [
                    int(
                        datetime.fromisoformat(day)
                        .replace(tzinfo=timezone.utc)
                        .timestamp()
                    )
                    for day, _ in rows
                ]
                closes = [close for _, close in rows]
                return httpx.Response(
                    200,
                    json={
                        "chart": {
                            "result": [
                                {
                                    "meta": {"symbol": symbol, "regularMarketPrice": closes[-1]},
                                    "timestamp": timestamps,
                                    "indicators": {"quote": [{"close": closes}]},
                                }
                            ],
                            "error": None,
                        }
                    },
                )
        return httpx.Response(
            200, json={"chart": {"result": None, "error": {"code": "Not Found"}}}
        )

    return YahooClient(transport=httpx.MockTransport(handler))


@pytest.fixture()
def settings():
    return FakeSettings()


@pytest.fixture()
def yahoo():
    return make_yahoo({"AAPL": [("2026-08-06", 150.0), ("2026-08-07", 152.5)]})


@pytest.fixture()
def client(settings, yahoo):
    app = create_app(
        database_url="sqlite://",
        yahoo=yahoo,
        settings_client=settings,
        enable_scheduler=False,
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def brokerage(client):
    return client.post(
        "/api/accounts",
        json={"name": "Fidelity", "type": "brokerage", "opening_cash": "10000.00"},
    ).json()


@pytest.fixture()
def rsu_account(client):
    return client.post("/api/accounts", json={"name": "Work RSUs", "type": "rsu"}).json()


@pytest.fixture()
def managed(client):
    return client.post(
        "/api/accounts",
        json={"name": "Smart Portfolio", "type": "managed", "opening_cash": "5000.00"},
    ).json()


@pytest.fixture()
def buy_aapl(client, brokerage):
    def _buy(quantity="10", price="150.00", date="2026-08-01"):
        response = client.post(
            "/api/transactions",
            json={
                "account_id": brokerage["id"],
                "type": "buy",
                "date": date,
                "symbol": "AAPL",
                "quantity": quantity,
                "price": price,
            },
        )
        assert response.status_code == 200, response.text
        return response.json()

    return _buy
