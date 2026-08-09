# Data model

Every table in every service database. Amounts are NUMERIC (Decimal in code),
signed: **positive = inflow, negative = outflow**, in the owning account's
currency. Dates are DATE unless noted. This file is the reference for reading
export JSON, whose top-level keys mirror these table names.

*(Filled in per service as each is built — kept in lockstep with
`app/models.py` of each service, which is the source of truth.)*

## settings (`sakura_settings`)

| table | columns | notes |
| ----- | ------- | ----- |
| settings | key (PK), value, is_secret, updated_at | `is_secret` rows are masked on list/read APIs |

Well-known keys are documented in `services/settings/README.md`.

## ledger (`sakura_ledger`)

Documented in phase 3–5 as built. Core tables: currencies, fx_rates, accounts,
categories, payees, payee_aliases, transactions, transaction_splits, bills,
bill_occurrences, import_profiles, import_batches, import_rows, transfer_rules.

## budget (`sakura_budget`)

Core tables: category_budgets, goals.

## stocks (`sakura_stocks`)

Core tables: investment_accounts, securities, lots, rsu_grants, vesting_events,
stock_transactions, prices, stock_import_profiles, analysis_results.

## receipts (`sakura_receipts`)

Core tables: documents, receipt_items.
