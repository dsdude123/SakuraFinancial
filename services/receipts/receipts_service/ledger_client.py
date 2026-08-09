"""Synchronous client for the ledger endpoints the receipts service needs."""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal

import httpx


class LedgerUnavailable(Exception):
    pass


class LedgerClient:
    def __init__(self, base_url: str | None = None, timeout: float = 10.0, transport=None):
        self.base_url = (base_url or os.environ.get("LEDGER_URL", "http://ledger:8001")).rstrip("/")
        self._http = httpx.Client(timeout=timeout, transport=transport)

    def _request(self, method: str, path: str, **kwargs):
        try:
            response = self._http.request(method, f"{self.base_url}{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise LedgerUnavailable(f"ledger service unreachable: {exc}") from exc
        if response.status_code >= 400:
            raise LedgerUnavailable(
                f"ledger returned {response.status_code}: {response.text[:300]}"
            )
        return response.json()

    def search_transactions(self, amount: Decimal, start: date, end: date) -> list[dict]:
        return self._request(
            "GET",
            "/api/transactions",
            params={
                "amount": str(abs(amount)),
                "start": start.isoformat(),
                "end": end.isoformat(),
                "limit": 50,
            },
        )

    def get_transaction(self, transaction_id: int) -> dict:
        return self._request("GET", f"/api/transactions/{transaction_id}")

    def replace_splits(self, transaction_id: int, splits: list[dict]) -> dict:
        return self._request(
            "PUT", f"/api/transactions/{transaction_id}/splits", json={"splits": splits}
        )

    def categories(self) -> list[dict]:
        return self._request("GET", "/api/categories")
