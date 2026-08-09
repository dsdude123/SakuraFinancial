"""Yahoo Finance market-data client.

Used by the stocks service for the daily price fetch, history backfill, and
on-demand fundamentals. This is the platform's only market-data feed and it is
strictly read-only public data — no account linkage of any kind, per the
requirements. Endpoints are the public query1 API (the same one yfinance
scrapes); everything degrades gracefully because Yahoo occasionally changes
things, and manual price entry always works.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx

BASE = "https://query1.finance.yahoo.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

QUOTE_SUMMARY_MODULES = "price,summaryDetail,defaultKeyStatistics,financialData,recommendationTrend"


class YahooError(Exception):
    pass


class YahooClient:
    def __init__(self, timeout: float = 20.0, transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(
            timeout=timeout, transport=transport, headers={"User-Agent": USER_AGENT}
        )

    def _get(self, path: str, params: dict) -> dict:
        try:
            response = self._http.get(f"{BASE}{path}", params=params)
        except httpx.HTTPError as exc:
            raise YahooError(f"Yahoo unreachable: {exc}") from exc
        if response.status_code != 200:
            raise YahooError(f"Yahoo returned {response.status_code} for {path}")
        try:
            return response.json()
        except ValueError as exc:
            raise YahooError("Yahoo returned non-JSON") from exc

    def daily_history(self, symbol: str, start: date, end: date) -> list[tuple[date, Decimal]]:
        """Daily closes in [start, end]. Missing days (holidays) are absent."""
        period1 = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
        period2 = int(
            datetime(end.year, end.month, end.day, 23, 59, tzinfo=timezone.utc).timestamp()
        )
        data = self._get(
            f"/v8/finance/chart/{symbol}",
            {"period1": period1, "period2": period2, "interval": "1d", "events": "history"},
        )
        try:
            result = data["chart"]["result"][0]
            timestamps = result.get("timestamp") or []
            closes = result["indicators"]["quote"][0].get("close") or []
        except (KeyError, IndexError, TypeError) as exc:
            error = (data.get("chart") or {}).get("error")
            raise YahooError(f"no chart data for {symbol!r}: {error}") from exc
        history: list[tuple[date, Decimal]] = []
        for stamp, close in zip(timestamps, closes):
            if close is None:
                continue
            day = datetime.fromtimestamp(stamp, tz=timezone.utc).date()
            history.append((day, Decimal(str(round(close, 4)))))
        return history

    def latest_close(self, symbol: str) -> tuple[date, Decimal]:
        """Most recent daily close (yesterday's close once a day is the whole
        requirement)."""
        data = self._get(
            f"/v8/finance/chart/{symbol}", {"range": "5d", "interval": "1d"}
        )
        try:
            result = data["chart"]["result"][0]
            timestamps = result.get("timestamp") or []
            closes = result["indicators"]["quote"][0].get("close") or []
        except (KeyError, IndexError, TypeError) as exc:
            raise YahooError(f"no chart data for {symbol!r}") from exc
        for stamp, close in zip(reversed(timestamps), reversed(closes)):
            if close is not None:
                day = datetime.fromtimestamp(stamp, tz=timezone.utc).date()
                return day, Decimal(str(round(close, 4)))
        raise YahooError(f"no recent close for {symbol!r}")

    def quote_summary(self, symbol: str) -> dict:
        """Fundamentals/analyst data, best-effort. Yahoo sometimes gates the
        quoteSummary endpoint behind cookies; when that fails we fall back to
        the chart metadata so analysis always has *something*."""
        try:
            data = self._get(
                f"/v10/finance/quoteSummary/{symbol}",
                {"modules": QUOTE_SUMMARY_MODULES},
            )
            result = data["quoteSummary"]["result"][0]
            return {"source": "quoteSummary", "symbol": symbol, "data": result}
        except (YahooError, KeyError, IndexError, TypeError):
            pass
        data = self._get(f"/v8/finance/chart/{symbol}", {"range": "1d", "interval": "1d"})
        try:
            meta = data["chart"]["result"][0]["meta"]
        except (KeyError, IndexError, TypeError) as exc:
            raise YahooError(f"no data at all for {symbol!r}") from exc
        return {"source": "chart_meta", "symbol": symbol, "data": meta, "fetched_at": time.time()}
