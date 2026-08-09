# Data model

Every table in every service database. Amounts are NUMERIC (Decimal in code)
and serialize through APIs/exports as **strings**; signs follow one rule
everywhere: **positive = inflow, negative = outflow**, in the owning
account's currency. Dates are DATE unless noted. Export JSON top-level keys
mirror these table names; `app models.py` files are the source of truth.

## settings (`sakura_settings`)

| table | key columns | notes |
| ----- | ----------- | ----- |
| settings | key (PK), value, is_secret, updated_at | secrets masked on read APIs; the mask sentinel `********` never overwrites a stored secret. Well-known keys: `services/settings/README.md` |

## ledger (`sakura_ledger`)

| table | notes |
| ----- | ----- |
| currencies | ISO code (PK), name, display decimals. Seeded: USD, CAD, TWD |
| fx_rates | manual rates (date, from, to, rate); lookups take the latest on/before a date and fall back to the inverse pair |
| accounts | name, type (`checking, savings, credit_card, cash, asset, liability`), currency, opening_balance, active. The first four types are cash-flow accounts |
| categories | name, parent (one level), kind (`expense`/`income`) as *default direction* — splits accept both signs so reimbursements net |
| payees | name, default_category_id (auto-fill), active |
| payee_aliases | normalized description pattern (`exact`/`prefix`/`contains`) → payee; learned during import review |
| transactions | account, date, payee, memo, status (`uncleared/cleared/reconciled`), kind (`normal/transfer/valuation`), transfer_group_id (pairs transfer legs), import_hash (dedup) |
| transaction_splits | the money: transaction, category (NULL for transfers/valuations/uncategorized), signed amount, memo. A transaction's total = sum of its splits |
| bills | payee, category, frequency (`weekly…annual`), expected amount, is_variable, next_due, active |
| bill_occurrences | bill, due_date (unique per bill), expected_amount, status (`upcoming/paid/skipped/amount_review`), matched_transaction_id, actual_amount |
| transfer_rules | description pattern → counterparty account; matching imports become transfers |
| import_profiles | name, default account, CSV config JSON (see `sakura_common.csvengine`) |
| import_batches / import_rows | the review stage: parsed rows with status (`ready/needs_payee/duplicate/transfer`), include flag, chosen payee/category, learn_alias, resulting transaction_id |

## budget (`sakura_budget`)

| table | notes |
| ----- | ----- |
| category_budgets | (month, category_id) → planned amount. Category IDs reference the ledger by API, not FK |
| goals | name, target_amount, monthly_contribution, target_date, priority, active |

Everything else (spent, carryovers, waterfall, General Fund) is derived from
ledger data at request time — never stored, never stale.

## stocks (`sakura_stocks`)

| table | notes |
| ----- | ----- |
| investment_accounts | name, type (`brokerage/rsu/managed`), opening_cash, trading window dates (RSU), active. Cash is derived: opening_cash + Σ transaction amounts |
| securities | symbol (unique), name, active |
| lots | account, security, quantity, total cost_basis, acquired_date, source (`buy/vest/manual`). FIFO unit for sells |
| rsu_grants / vesting_events | grant (account, security, grant_date) with dated share tranches; released events link the lot they created |
| stock_transactions | type (`buy/sell/dividend/vest/deposit/withdraw/fee`), signed cash `amount`, quantity/price/fees, realized_gain (sells), import_hash |
| prices | (security, date) → close, source (`yahoo/manual`); manual wins over yahoo. Kept forever |
| stock_import_profiles | CSV config JSON incl. the **action_map** (broker strings → internal actions) |
| stock_import_batches / stock_import_rows | review stage; rows are `ready` or `duplicate` |
| analysis_results | stored on-demand analyses: holdings/valuation/price summaries/Yahoo fundamentals JSON + optional ai_text |

## receipts (`sakura_receipts`)

| table | notes |
| ----- | ----- |
| documents | filename, content_type, stored_name (file on the receipts volume — kept forever), vendor, doc_date, total, currency, status (`new/parsed/needs_review/linked/ignored`), extracted_text, parse_note, linked_transaction_id (the ONE ledger transaction this receipt documents) |
| receipt_items | document line items: description, amount, category guess (name + resolved ledger id). Applying a split turns these into the linked transaction's `transaction_splits` via the ledger API |

## Cross-service references

There are no cross-database foreign keys. Budget/receipts reference ledger
category and transaction IDs over the API; the Monthly Updates page stores
skip flags in settings keys (`monthly.skip.<yyyy-mm>.<service>.<account_id>`).
Because exports preserve primary keys, restoring all services from one backup
keeps every cross-service link intact.
