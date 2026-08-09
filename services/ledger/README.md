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

Bills and CSV import endpoints are documented in the sections of this README
added with their phases (see also `docs/data-model.md`).

## Delete semantics

Anything referenced by transactions (accounts, categories, payees) refuses
deletion with 409 and should be deactivated instead — history stays intact
forever.
