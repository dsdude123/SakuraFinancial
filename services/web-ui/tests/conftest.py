"""Full-stack test rig: the web UI runs against REAL ledger, budget, and
settings apps booted in-process on SQLite, wired together with httpx ASGI
transports. No mocks of our own APIs — if a page works here, it works on the
compose stack."""

import httpx
import pytest
from fastapi.testclient import TestClient

from budget_service.ledger_client import LedgerClient
from budget_service.main import create_app as create_budget_app
from ledger_service.main import create_app as create_ledger_app
from receipts_service.ledger_client import LedgerClient as ReceiptsLedgerClient
from receipts_service.main import create_app as create_receipts_app
from settings_service.main import create_app as create_settings_app
from stocks_service.main import create_app as create_stocks_app
from webui_service.clients import Clients
from webui_service.main import create_app as create_webui_app


class SyncASGITransport(httpx.BaseTransport):
    """Lets a synchronous httpx client (budget's ledger client) call an ASGI
    app in-process."""

    def __init__(self, app):
        self._client = TestClient(app)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self._client.request(
            request.method,
            str(request.url),
            headers=dict(request.headers),
            content=request.content,
        )
        return httpx.Response(
            response.status_code, headers=response.headers, content=response.content
        )


def down_service_handler(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("service not part of this test rig")


def make_fake_yahoo():
    """Offline Yahoo: AAPL exists with two closes; everything else is unknown."""
    from datetime import datetime, timezone

    rows = [("2026-08-06", 150.0), ("2026-08-07", 152.5)]

    def handler(request: httpx.Request) -> httpx.Response:
        if "/chart/AAPL" in str(request.url):
            timestamps = [
                int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())
                for day, _ in rows
            ]
            return httpx.Response(
                200,
                json={
                    "chart": {
                        "result": [
                            {
                                "meta": {"symbol": "AAPL", "regularMarketPrice": rows[-1][1]},
                                "timestamp": timestamps,
                                "indicators": {"quote": [{"close": [c for _, c in rows]}]},
                            }
                        ],
                        "error": None,
                    }
                },
            )
        return httpx.Response(200, json={"chart": {"result": None, "error": {"code": "404"}}})

    from sakura_common.yahoo import YahooClient

    return YahooClient(transport=httpx.MockTransport(handler))


@pytest.fixture()
def stack(tmp_path):
    ledger_app = create_ledger_app(database_url="sqlite://")
    settings_app = create_settings_app(database_url="sqlite://")
    budget_app = create_budget_app(
        database_url="sqlite://",
        ledger_client=LedgerClient(
            base_url="http://ledger", transport=SyncASGITransport(ledger_app)
        ),
    )

    class SettingsViaApp:
        def __init__(self, app):
            self._client = TestClient(app)

        def get(self, key, default=None):
            response = self._client.get(f"/api/settings/{key}", params={"reveal": "true"})
            if response.status_code != 200:
                return default
            return response.json().get("value", default)

    stocks_app = create_stocks_app(
        database_url="sqlite://",
        yahoo=make_fake_yahoo(),
        settings_client=SettingsViaApp(settings_app),
        enable_scheduler=False,
    )
    receipts_app = create_receipts_app(
        database_url="sqlite://",
        ledger_client=ReceiptsLedgerClient(
            base_url="http://ledger", transport=SyncASGITransport(ledger_app)
        ),
        settings_client=SettingsViaApp(settings_app),
        data_dir=str(tmp_path / "receipts-data"),
    )
    webui_app = create_webui_app(
        clients=Clients(
            transports={
                "ledger": httpx.ASGITransport(app=ledger_app),
                "budget": httpx.ASGITransport(app=budget_app),
                "settings": httpx.ASGITransport(app=settings_app),
                "stocks": httpx.ASGITransport(app=stocks_app),
                "receipts": httpx.ASGITransport(app=receipts_app),
            }
        ),
        secret_key="test-secret",
    )
    return {
        "ledger": ledger_app,
        "budget": budget_app,
        "settings": settings_app,
        "stocks": stocks_app,
        "receipts": receipts_app,
        "webui": webui_app,
    }


@pytest.fixture()
def browser(stack):
    """An anonymous browser session."""
    with TestClient(stack["webui"], follow_redirects=False) as client:
        yield client


@pytest.fixture()
def logged_in(browser):
    """A browser that has completed first-run password setup."""
    response = browser.post(
        "/setup-password", data={"password": "hunter22", "password2": "hunter22"}
    )
    assert response.status_code == 303
    return browser


@pytest.fixture()
def ledger_api(stack):
    """Direct access to the ledger API for seeding test data."""
    return TestClient(stack["ledger"])
