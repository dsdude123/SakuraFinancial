import json
from datetime import date
from decimal import Decimal

import httpx
import pytest

from sakura_common.yahoo import YahooClient, YahooError


def chart_payload(timestamps, closes, meta=None):
    return {
        "chart": {
            "result": [
                {
                    "meta": meta or {"symbol": "AAPL"},
                    "timestamp": timestamps,
                    "indicators": {"quote": [{"close": closes}]},
                }
            ],
            "error": None,
        }
    }


def make_client(handler):
    return YahooClient(transport=httpx.MockTransport(handler))


class TestDailyHistory:
    def test_parses_closes_and_skips_nulls(self):
        # 2026-08-05 and 2026-08-07 (UTC midnights); null close skipped
        def handler(request):
            assert "/v8/finance/chart/AAPL" in str(request.url)
            assert request.headers["user-agent"].startswith("Mozilla")
            return httpx.Response(
                200,
                json=chart_payload([1786233600, 1786320000, 1786406400], [150.0, None, 151.5567]),
            )

        history = make_client(handler).daily_history("AAPL", date(2026, 8, 1), date(2026, 8, 9))
        assert len(history) == 2
        assert history[0][1] == Decimal("150.0")
        assert history[1][1] == Decimal("151.5567")

    def test_unknown_symbol_raises(self):
        def handler(request):
            return httpx.Response(
                200,
                json={"chart": {"result": None, "error": {"code": "Not Found"}}},
            )

        with pytest.raises(YahooError, match="no chart data"):
            make_client(handler).daily_history("NOPE", date(2026, 8, 1), date(2026, 8, 9))

    def test_http_error_raises(self):
        client = make_client(lambda request: httpx.Response(429, text="rate limited"))
        with pytest.raises(YahooError, match="429"):
            client.daily_history("AAPL", date(2026, 8, 1), date(2026, 8, 9))


class TestLatestClose:
    def test_picks_last_non_null(self):
        client = make_client(
            lambda request: httpx.Response(
                200, json=chart_payload([1786233600, 1786320000], [150.0, None])
            )
        )
        day, close = client.latest_close("AAPL")
        assert close == Decimal("150.0")


class TestQuoteSummary:
    def test_quote_summary_happy_path(self):
        def handler(request):
            if "/v10/finance/quoteSummary/" in str(request.url):
                return httpx.Response(
                    200,
                    json={
                        "quoteSummary": {
                            "result": [{"price": {"regularMarketPrice": {"raw": 150.0}}}]
                        }
                    },
                )
            raise AssertionError("should not fall back")

        summary = make_client(handler).quote_summary("AAPL")
        assert summary["source"] == "quoteSummary"

    def test_falls_back_to_chart_meta_when_gated(self):
        def handler(request):
            if "/v10/finance/quoteSummary/" in str(request.url):
                return httpx.Response(401, text=json.dumps({"finance": {"error": "auth"}}))
            return httpx.Response(
                200,
                json=chart_payload([1786233600], [150.0], meta={"regularMarketPrice": 150.0}),
            )

        summary = make_client(handler).quote_summary("AAPL")
        assert summary["source"] == "chart_meta"
        assert summary["data"]["regularMarketPrice"] == 150.0
