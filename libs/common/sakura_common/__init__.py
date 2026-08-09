"""sakura_common: shared building blocks for all SakuraFinancial services.

Modules:
    money           Decimal-safe parsing/formatting of monetary amounts.
    dedup           Description normalization and import-row dedup hashing.
    jsonutil        JSON encode/decode that round-trips Decimal and dates.
    csvengine       Profile-driven CSV parsing with all-or-nothing validation.
    llm             Provider-agnostic LLM client (Anthropic / OpenAI-compatible).
    yahoo           Yahoo Finance market data client (prices, quote summary).
    settings_client Client for the settings-service key-value store.
"""

__all__ = [
    "money",
    "dedup",
    "jsonutil",
    "csvengine",
    "llm",
    "yahoo",
    "settings_client",
]
