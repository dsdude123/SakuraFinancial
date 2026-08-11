"""Async HTTP clients for the backend services.

The web UI holds no data of its own — every page is assembled from service
APIs at request time. Handlers are async so tests can wire these clients
straight into in-process service apps via httpx.ASGITransport (full-stack
integration tests with no network).
"""

from __future__ import annotations

import os

import httpx


class ServiceError(Exception):
    """A backend call failed. Carries enough to render a friendly page."""

    def __init__(self, service: str, status: int | None, detail):
        self.service = service
        self.status = status
        self.detail = detail
        super().__init__(f"{service}: {status} {detail}")


class ServiceClient:
    def __init__(self, name: str, base_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(timeout=30.0, transport=transport)

    async def request(self, method: str, path: str, **kwargs):
        try:
            response = await self._http.request(method, f"{self.base_url}{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise ServiceError(self.name, None, f"unreachable ({exc})") from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise ServiceError(self.name, response.status_code, detail)
        if response.headers.get("content-type", "").startswith("application/json"):
            return response.json()
        return response.content

    async def get(self, path: str, params: dict | None = None):
        return await self.request("GET", path, params=params)

    async def post(self, path: str, json=None):
        return await self.request("POST", path, json=json)

    async def put(self, path: str, json=None):
        return await self.request("PUT", path, json=json)

    async def delete(self, path: str):
        return await self.request("DELETE", path)


class Clients:
    """One bundle with a client per service, injected into app.state."""

    def __init__(self, transports: dict[str, httpx.AsyncBaseTransport] | None = None):
        transports = transports or {}

        def make(name: str, env: str, default: str) -> ServiceClient:
            return ServiceClient(
                name, os.environ.get(env, default), transport=transports.get(name)
            )

        self.ledger = make("ledger", "LEDGER_URL", "http://ledger:8001")
        self.budget = make("budget", "BUDGET_URL", "http://budget:8002")
        self.stocks = make("stocks", "STOCKS_URL", "http://stocks:8003")
        self.receipts = make("receipts", "RECEIPTS_URL", "http://receipts:8004")
        self.settings = make("settings", "SETTINGS_URL", "http://settings:8005")
