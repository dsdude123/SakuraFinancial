# ledger-service (:8001)

The core of SakuraFinancial: accounts, categories, payees, transactions,
transfers, bills, and CSV import. Every other service derives from data held
here. Live OpenAPI docs at `/docs` on the service port.

## Concepts

- **Sign convention:** positive = inflow, negative = outflow, always in the
  owning account's currency. Amounts travel through APIs as strings.
- **Accounts** (`checking, savings, credit_card, cash, asset, liability`) each
  have a currency — a CAD wallet or TWD cash stash is just a `cash` account in
  that currency. The first four types are *cash-flow* accounts; `asset` /
  `liability` only ever affect net worth.
- **Transactions & splits:** a transaction's total is the sum of its splits;
  each split points at a category (or none, for uncategorized). Splitting a
  grocery receipt across categories re-divides ONE transaction — never
  creates more.
- **Subcategories:** categories nest one level (Food → Groceries, Dining Out).
  Children must match their parent's kind. APIs return a `path`
  ("Food: Groceries") for display and list parents immediately followed by
  their children. `GET /api/reports/category-tree` rolls children up into
  their parent's total (with the parent's own direct spending kept separately
  as `own_net`), which is what drives the drill-down spending report;
  `/api/reports/category-actuals` still returns flat per-category rows.
- **Reimbursements:** categories have a default direction (`kind`) but accept
  both signs. An inflow against "Rent" reduces net Rent spending in every
  report — this is deliberate and central.
- **Transfers** are paired transactions sharing `transfer_group_id`, splits
  have no category, and they never appear in spending/cash-flow reports.
  Deleting one leg deletes both.
- **Valuations:** `POST /api/accounts/{id}/valuation` records an asset's new
  value as a `kind='valuation'` transaction (the delta). Net worth sees it;
  monthly cash flow never does.

## Key endpoints

| Area | Endpoints |
| ---- | --------- |
| Accounts | `GET/POST /api/accounts`, `GET/PUT/DELETE /api/accounts/{id}`, `POST /api/accounts/{id}/valuation` |
| Categories | `GET/POST /api/categories`, `PUT/DELETE /api/categories/{id}`, `POST /api/seed-defaults` (first run only) |
| Payees | `GET/POST /api/payees`, `GET /api/payees/{id}` (incl. auto-fill data), aliases under `/api/payees/{id}/aliases` |
| Transactions | `GET/POST /api/transactions` (rich filters: account, date range, category, payee, `q`, exact `amount`, `uncategorized`), `PUT /api/transactions/{id}`, `PUT /api/transactions/{id}/splits`, `POST /api/transfers` |
| FX | `GET/POST /api/fx`, `GET /api/fx/rate` (manual rates; inverse pairs resolve automatically) |
| Reports | `/api/reports/category-actuals`, `/api/reports/cashflow`, `/api/reports/net-worth` |
| Backup | `GET /api/export`, `POST /api/import` (full replace, IDs preserved) |

## CSV import

Per-institution **import profiles** (`/api/import/profiles`) describe the file
shape: delimiter, header handling, column mapping (single-amount or
debit/credit), date format, sign convention. Config keys are documented in
`sakura_common.csvengine`.

The pipeline is **all-or-nothing**: `POST /api/import/preview` validates the
entire file first; any bad row (unparseable amount, wrong date format, missing
column) rejects the upload with a complete row-by-row error report and writes
NOTHING. A valid file becomes a review batch whose rows are classified:

| Row status | Meaning |
| ---------- | ------- |
| `duplicate` | Row hash (account+date+amount+normalized description) already imported — excluded by default, can be forced back in |
| `transfer` | A transfer rule matched (e.g. descriptions starting `VENMO` → transfer to the configured account) |
| `ready` | A learned payee alias matched; payee + default category prefilled |
| `needs_payee` | Unknown description — pick or create a payee (`PUT /api/import/rows/{id}`); the choice is learned as an alias so next month maps automatically |

`POST /api/import/batches/{id}/commit` turns included rows into cleared
transactions (transfer rows become paired transfer legs), learns aliases, runs
**bill matching** on every new transaction (fixed-amount mismatches surface in
the commit summary as `amount_review`), and pings the receipts service to link
waiting receipts. Batches also seed newly added accounts with history.

Transfer rules live at `/api/transfer-rules`.

## Bills

Bills (`/api/bills`) carry a payee, category, frequency (`weekly` …
`annual`), an expected amount, and an `is_variable` flag. Creating one
materializes `bill_occurrences` ~13 months ahead.

- **Matching** runs on every new normal transaction — manual or imported:
  same payee, due date within ±10 days, closest occurrence wins. Variable
  bills accept any amount; a fixed bill matched at a different amount flags
  the occurrence **`amount_review`**, which the UI turns into the prompt
  *"Electric was $150.00, expected $142.19 — update the bill?"*.
  `POST /api/bills/occurrences/{id}/resolve` with `update_bill` adopts the
  new amount on the bill and all upcoming occurrences; `keep` treats it as a
  one-off. Unresolved reviews stay visible on the bills overview.
- **Accrual** (`GET /api/bills/accrual`): every bill reduced to a *monthly
  load* (annual/12, quarterly/3, weekly×52/12 …), totals grouped by category,
  and per-bill set-aside progress — the December insurance premium shows up
  as $100/month all year, with "you should have $800 set aside by now" in
  August.
- Occurrence management: list/filter (`/api/bills/occurrences`), `skip`,
  `unmatch` (undo a wrong match).

See also `docs/data-model.md`.

## Delete semantics

Anything referenced by transactions (accounts, categories, payees) refuses
deletion with 409 and should be deactivated instead — history stays intact
forever.
