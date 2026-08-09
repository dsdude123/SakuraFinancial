"""HTTP client for the ledger service (the budget's single source of truth
for actual money movement). Tests substitute a fake with the same surface."""

from __future__ import annotations

import os
from datetime import date

import httpx


class LedgerUnavailable(Exception):
    pass


class LedgerClient:
    def __init__(self, base_url: str | None = None, timeout: float = 10.0, transport=None):
        self.base_url = (base_url or os.environ.get("LEDGER_URL", "http://ledger:8001")).rstrip("/")
        self._http = httpx.Client(timeout=timeout, transport=transport)

    def _get(self, path: str, params: dict | None = None):
        try:
            response = self._http.get(f"{self.base_url}{path}", params=params)
        except httpx.HTTPError as exc:
            raise LedgerUnavailable(f"ledger service unreachable: {exc}") from exc
        if response.status_code != 200:
            raise LedgerUnavailable(
                f"ledger returned {response.status_code}: {response.text[:300]}"
            )
        return response.json()

    def category_actuals(self, start: date, end: date) -> list[dict]:
        return self._get(
            "/api/reports/category-actuals",
            {"start": start.isoformat(), "end": end.isoformat()},
        )

    def bills_accrual(self, on: date | None = None) -> dict:
        params = {"on": on.isoformat()} if on else None
        return self._get("/api/bills/accrual", params)

    def categories(self) -> list[dict]:
        return self._get("/api/categories")
