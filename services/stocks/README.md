# stocks-service (:8003)

Investment accounts, daily price history, RSUs, and on-demand analysis.
Live OpenAPI docs at `/docs`.

## Account types

| Type | Behavior |
| ---- | -------- |
| `brokerage` | Buy/sell freely; lots tracked with FIFO selling and realized gains |
| `rsu` | Grants with vesting schedules; vested shares release into lots at vest-day market value; optional trading-window dates surfaced in the UI |
| `managed` | E*TRADE-smart-portfolio style: the user only moves cash in/out; holdings arrive via CSV import; the UI hides trade entry and the API refuses analysis (you can't act on it) — performance tracking only |

Cash is derived (`opening_cash` + signed cash effects of transactions); lots
carry their own cost basis, so unrealized/realized gains are exact.

Cash that came from a bank account arrives through
`POST /api/transfers/external`: an ordinary `deposit`/`withdraw` that also
carries the ledger leg's `transfer_group_id` and the bank account's ref
(`external_account="bank:3"`). `DELETE /api/transfers/external/{group}` removes
this side when the ledger's leg goes, so a transfer never survives as half of
itself. The full picture is in `docs/architecture.md`.

## Prices

- **Daily fetch**: an in-process APScheduler job pulls the previous close
  from Yahoo Finance for every active security once a day (hour configurable
  via settings key `stocks.fetch_hour_utc`, default 10:00 UTC). Manual
  trigger: `POST /api/prices/refresh` (the UI button).
- **Backfill**: adding a security can pull ~5 years of history
  (`POST /api/prices/backfill/{symbol}`) so charts have depth immediately.
- **Manual entry** always works (`POST /api/prices`) and manual prices are
  never overwritten by the daily fetch.
- History is kept forever in the `prices` table — this fixes the MS Money
  problem where history only existed from the day you typed it in.

## CSV import

Same all-or-nothing pipeline as bank imports, plus each profile's
**action map**: the broker's transaction-type strings (`"Bought"`,
`"YOU SOLD"`, `"WIRE IN"`...) map to internal actions
(`buy/sell/dividend/vest/deposit/withdraw/fee/ignore`). An unmapped string
rejects the whole file with instructions to extend the map — nothing imports
until every row resolves. Committed rows apply full portfolio effects
(lots, FIFO, cash) oldest-first; re-uploads dedupe by row hash.

## Analysis (on demand only)

`POST /api/analyze {account_id}` gathers holdings, 52-week price summaries,
and Yahoo fundamentals (quoteSummary, falling back to chart metadata when
Yahoo gates it), then — if an LLM is configured in Settings — asks for
guidance with the user's stored trading philosophy
(`stocks.ai_instructions`) leading the prompt. Results are stored
(`/api/analyses`). No LLM configured → fundamentals only, clearly marked.
Nothing is ever scheduled.

## Other endpoints

Accounts CRUD, transactions (`buy/sell/dividend/vest/deposit/withdraw/fee`),
`POST /api/transfers/external` + `DELETE /api/transfers/external/{group}`
(bank <-> brokerage cash),
`/api/valuation` and `/api/valuation/series` (feeds the net-worth report),
RSU grants + `POST /api/rsu/vests/{id}/release`, `GET/POST /api/export|import`
for backup.
