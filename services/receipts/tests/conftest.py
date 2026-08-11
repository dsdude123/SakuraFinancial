"""Receipts tests run against a REAL ledger app in-process (SQLite) so
matching and split application exercise the actual ledger rules."""

import httpx
import pytest
from fastapi.testclient import TestClient

from ledger_service.main import create_app as create_ledger_app
from receipts_service.ledger_client import LedgerClient
from receipts_service.main import create_app as create_receipts_app


class SyncASGITransport(httpx.BaseTransport):
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


class FakeSettings:
    def __init__(self):
        self.values: dict[str, str] = {}

    def get(self, key, default=None):
        return self.values.get(key, default)


@pytest.fixture()
def ledger_app():
    return create_ledger_app(database_url="sqlite://")


@pytest.fixture()
def ledger_api(ledger_app):
    return TestClient(ledger_app)


@pytest.fixture()
def settings():
    return FakeSettings()


@pytest.fixture()
def client(ledger_app, settings, tmp_path):
    app = create_receipts_app(
        database_url="sqlite://",
        ledger_client=LedgerClient(
            base_url="http://ledger", transport=SyncASGITransport(ledger_app)
        ),
        settings_client=settings,
        data_dir=str(tmp_path / "receipts-data"),
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def checking(ledger_api):
    return ledger_api.post(
        "/api/accounts", json={"name": "Checking", "type": "checking"}
    ).json()


@pytest.fixture()
def groceries(ledger_api):
    return ledger_api.post("/api/categories", json={"name": "Groceries", "kind": "expense"}).json()


@pytest.fixture()
def household(ledger_api):
    return ledger_api.post("/api/categories", json={"name": "Household", "kind": "expense"}).json()


@pytest.fixture()
def upload(client):
    def _upload(content: bytes, filename="receipt.txt", content_type="text/plain"):
        return client.post("/api/documents", files={"file": (filename, content, content_type)})

    return _upload
