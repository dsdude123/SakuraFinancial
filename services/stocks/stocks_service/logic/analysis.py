"""On-demand analysis: Yahoo fundamentals plus optional LLM guidance.

Strictly on demand — no scheduled AI calls, per the requirements. The prompt
always includes the user's stored trading philosophy (settings key
``stocks.ai_instructions``) so guidance matches how they actually invest,
e.g. long-term holding and tax awareness.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from sakura_common.llm import LLMClient, LLMError, LLMNotConfigured, config_from_settings
from sakura_common.yahoo import YahooClient, YahooError

from ..models import AnalysisResult, InvestmentAccount, Price, Security
from . import portfolio

AI_INSTRUCTIONS_KEY = "stocks.ai_instructions"

SYSTEM_PROMPT = (
    "You are an investment analysis assistant inside a self-hosted personal "
    "finance app. You are given portfolio holdings, price history summaries, "
    "and fundamentals data. Give a concise, practical assessment: what looks "
    "healthy, what deserves attention, and what actions (if any) the user "
    "could consider. You are not a licensed financial advisor and must say "
    "so briefly. Respect the user's stated trading philosophy above all. "
    "Format as short plain-text sections; no markdown tables."
)


def price_summary(db: Session, security: Security) -> dict:
    year_ago = date.today() - timedelta(days=365)
    rows = db.execute(
        select(Price)
        .where(Price.security_id == security.id, Price.date >= year_ago)
        .order_by(Price.date)
    ).scalars().all()
    if not rows:
        return {"symbol": security.symbol, "history": "none"}
    closes = [row.close for row in rows]
    return {
        "symbol": security.symbol,
        "latest": str(closes[-1]),
        "latest_date": rows[-1].date.isoformat(),
        "high_52w": str(max(closes)),
        "low_52w": str(min(closes)),
        "year_change_pct": str(
            ((closes[-1] - closes[0]) / closes[0] * 100).quantize(Decimal("0.1"))
        )
        if closes[0]
        else None,
    }


def gather_fundamentals(db: Session, yahoo: YahooClient, symbols: list[str]) -> dict:
    data: dict[str, dict] = {}
    for symbol in symbols:
        try:
            data[symbol] = yahoo.quote_summary(symbol)
        except YahooError as exc:
            data[symbol] = {"error": str(exc)}
    return data


def analyze_account(
    db: Session,
    yahoo: YahooClient,
    settings_client,
    account: InvestmentAccount,
) -> AnalysisResult:
    positions = portfolio.holdings(db, account)
    symbols = [position["symbol"] for position in positions]
    securities = {
        s.symbol: s
        for s in db.execute(select(Security).where(Security.symbol.in_(symbols))).scalars()
    }
    summaries = [price_summary(db, securities[symbol]) for symbol in symbols if symbol in securities]
    fundamentals = gather_fundamentals(db, yahoo, symbols)
    valuation = portfolio.account_valuation(db, account)

    ai_text: str | None = None
    try:
        config = config_from_settings(settings_client)
        instructions = settings_client.get(AI_INSTRUCTIONS_KEY) or "(none provided)"
        prompt = (
            f"USER'S TRADING PHILOSOPHY:\n{instructions}\n\n"
            f"ACCOUNT: {account.name} (type: {account.type}, cash {valuation['cash']}, "
            f"holdings value {valuation['market_value']})\n\n"
            f"HOLDINGS:\n{positions}\n\n"
            f"PRICE HISTORY (52 weeks):\n{summaries}\n\n"
            f"FUNDAMENTALS (Yahoo, may be partial):\n{_trim_fundamentals(fundamentals)}\n\n"
            "Give your assessment and any suggested actions consistent with the "
            "philosophy above."
        )
        ai_text = LLMClient(config).complete(prompt, system=SYSTEM_PROMPT, max_tokens=1500)
    except LLMNotConfigured:
        ai_text = None
    except LLMError as exc:
        ai_text = f"(AI analysis failed: {exc})"

    result = AnalysisResult(
        account_id=account.id,
        symbol="",
        fundamentals={
            "holdings": positions,
            "valuation": valuation,
            "price_summaries": summaries,
            "yahoo": _trim_fundamentals(fundamentals),
        },
        ai_text=ai_text,
    )
    db.add(result)
    db.flush()
    return result


def _trim_fundamentals(fundamentals: dict) -> dict:
    """Yahoo quoteSummary payloads are huge; keep the parts a human (or a
    prompt) actually reads."""
    KEEP = {
        "price": ("regularMarketPrice", "marketCap", "shortName", "currency"),
        "summaryDetail": (
            "trailingPE",
            "forwardPE",
            "dividendYield",
            "fiftyTwoWeekHigh",
            "fiftyTwoWeekLow",
            "beta",
        ),
        "defaultKeyStatistics": ("trailingEps", "forwardEps", "pegRatio", "priceToBook"),
        "financialData": (
            "recommendationKey",
            "targetMeanPrice",
            "returnOnEquity",
            "profitMargins",
            "totalCashPerShare",
            "debtToEquity",
        ),
        "recommendationTrend": ("trend",),
    }
    trimmed: dict[str, dict] = {}
    for symbol, payload in fundamentals.items():
        if "error" in payload:
            trimmed[symbol] = payload
            continue
        if payload.get("source") != "quoteSummary":
            trimmed[symbol] = payload  # chart_meta fallback is already small
            continue
        data = payload.get("data", {})
        keep: dict = {"source": "quoteSummary"}
        for module, keys in KEEP.items():
            if module in data and isinstance(data[module], dict):
                keep[module] = {k: data[module].get(k) for k in keys if k in data[module]}
        trimmed[symbol] = keep
    return trimmed
