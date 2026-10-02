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
| categories | name, parent (one level: Food → Groceries), kind (`expense`/`income`) as *default direction* — splits accept both signs so reimbursements net. APIs return a `path` ("Food: Groceries") and list parents immediately followed by their children |
| payees | name, default_category_id (auto-fill), active |
| payee_aliases | description pattern (`exact`/`regex`/`prefix`/`contains`) → payee; learned during import review — from the payee the reviewer picks, or from the description itself when a row is committed with none (that one never repoints an alias the user set deliberately). Literal patterns are stored normalized; **regex patterns are stored verbatim** and matched case-insensitively (normalizing one would turn `\d` into `\D`). On a tie the most deliberate type wins, in that order |
| transactions | account, date, payee, memo, status (`uncleared/cleared/reconciled`), kind (`normal/transfer/valuation`), transfer_group_id (pairs transfer legs), external_account (only on a transfer whose other side is *not* a ledger account — `"stock:1"`, an investment account in the stocks service; such a transfer has one leg here and the stocks row with the same transfer_group_id is the other), import_hash (dedup) |
| transaction_splits | the money: transaction, category (NULL for transfers/valuations/uncategorized), signed amount, memo. A transaction's total = sum of its splits |
| bills | payee, category, frequency (`weekly…annual`), expected amount, is_variable, next_due, active |
| bill_occurrences | bill, due_date (unique per bill), expected_amount, status (`upcoming/paid/skipped/amount_review`), matched_transaction_id, actual_amount |
| transfer_rules | description pattern → counterparty account; matching imports become transfers. The counterparty is **either** a ledger account (`account_id`) or an account another service owns (`external_account`, `"stock:1"`) — exactly one; an external rule imports the row as a one-legged transfer whose far side the caller settles there. `match_days` is how far apart the two banks may date the same transfer: when the other account's statement is imported later, a row matching an already-recorded transfer by amount inside that window is flagged `counterpart` and left out rather than booking the move twice (for an external rule the already-recorded leg is matched by its own `external_account`, since there is no second ledger row) |
| import_profiles | name, default account, CSV config JSON (see `sakura_common.csvengine`) |
| import_batches / import_rows | the review stage (re-runnable: `POST /api/import/batches/{id}/reclassify` re-examines rows still `needs_payee` against aliases and transfer rules added since upload, leaving answered rows alone): parsed rows with status (`ready/needs_payee/duplicate/transfer/counterpart`), include flag, chosen payee/category or transfer counterparty (`transfer_account_id`, or `transfer_external_account` for an investment account), learn_alias, resulting transaction_id. `duplicate` means *this account already imported that row* — a file is never deduplicated against itself, so two identical same-day transactions both land |

## budget (`sakura_budget`)

| table | notes |
| ----- | ----- |
| category_budgets | (month, category_id) → planned amount, set on a parent or a child. Rows are **change points, not per-month values**: an amount holds from its month onward until a later row supersedes it, so a budget is entered once rather than every month. `amount = 0` is a stop ("no longer budgeted from here on"), and deleting a row reverts that month to whatever the previous change said. `logic.resolve_plan` does the resolution. Category IDs reference the ledger by API, not FK |
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
| stock_transactions | type (`buy/sell/dividend/vest/deposit/withdraw/fee`), signed cash `amount`, quantity/price/fees, realized_gain (sells), import_hash. A `deposit`/`withdraw` that is the far side of a bank transfer also carries transfer_group_id and external_account (`"bank:3"`, a ledger account); cash that simply appeared leaves both NULL |
| prices | (security, date) → close, source (`yahoo/manual`); manual wins over yahoo. Kept forever |
| stock_import_profiles | CSV config JSON incl. the **action_map** (broker strings → internal actions) |
| stock_import_batches / stock_import_rows | review stage; rows are `ready`, `duplicate` (already imported into this account), `no_lots` (a sale of shares the account doesn't hold — bought before the statement's window), or `counterpart` (a cash row that is the far side of a bank transfer already booked here, matched by amount within `transfer_match_days` of the profile config, default 5) |
| analysis_results | stored on-demand analyses: holdings/valuation/price summaries/Yahoo fundamentals JSON + optional ai_text |

## receipts (`sakura_receipts`)

| table | notes |
| ----- | ----- |
| documents | filename, content_type, stored_name (file on the receipts volume — kept forever), vendor, doc_date, total, currency, status (`new/parsed/needs_review/linked/ignored`), extracted_text, parse_note, linked_transaction_id (the ONE ledger transaction this receipt documents) |
| receipt_items | document line items: description, amount, category guess (name + resolved ledger id). Applying a split turns these into the linked transaction's `transaction_splits` via the ledger API |

## Cross-service references

There are no cross-database foreign keys. A transfer between a bank account and
an investment account is therefore two rows in two databases — a categoryless
ledger transfer leg and a stocks `deposit`/`withdraw` — tied together by a
shared `transfer_group_id` plus each side's `external_account` ref
(`"bank:<id>"` / `"stock:<id>"`). The web UI writes both and undoes the first
if the second fails; see `docs/architecture.md`.

Budget/receipts reference ledger
category and transaction IDs over the API; the Monthly Updates page stores
skip flags in settings keys (`monthly.skip.<yyyy-mm>.<service>.<account_id>`).
Because exports preserve primary keys, restoring all services from one backup
keeps every cross-service link intact.
