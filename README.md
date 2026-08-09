# SakuraFinancial

A self-hosted personal finance platform that replaces Microsoft Money 2000 —
transactions, budgets, bills, stocks, goals, and receipts — built as a Docker
microservices stack with a deliberately retro, **IE6-compatible** web UI
(server-side rendering only; it must work on Windows 98).

Design principles:

- **No connections to financial institutions.** Data enters by hand, by CSV
  upload, or by receipt upload. Market data (stock prices) is the only outside
  feed, and AI features are optional and pluggable.
- **Server-side everything.** HTML 4.01, table layouts, zero required
  JavaScript, charts rendered to PNG on the server.
- **Own your data.** Every service exports human-readable JSON; a single
  Backup page produces a restorable archive.
- Everything documented, everything tested.

## Services

| Service  | Port | Purpose |
| -------- | ---- | ------- |
| web-ui   | 8000 | IE6-compatible SSR frontend, charts, backup/restore (the only published port) |
| ledger   | 8001 | Accounts, payees, categories, transactions & splits, transfers, bills, CSV import |
| budget   | 8002 | Envelope budgets, auto-assignment waterfall, deficits, savings goals |
| stocks   | 8003 | Investment/RSU/managed accounts, daily price history, on-demand analysis |
| receipts | 8004 | Receipt/invoice upload, OCR + LLM parsing, transaction matching |
| settings | 8005 | Key-value configuration store (API keys, provider choices) |
| db       | —    | PostgreSQL 16; one database + one user per service |

All inter-service traffic stays on the internal Docker network.

## Quick start

```sh
cp .env.example .env      # then edit the passwords
docker compose up --build
```

Browse to `http://<host>:8000`. On first visit you'll be asked to set the
login password. Everything else — LLM API keys, import profiles, FX rates —
is configured on the Settings pages inside the app.

> The UI is served over plain HTTP on your LAN because IE6 on Windows 98
> cannot negotiate modern TLS. Do not expose the port to the internet; see
> `docs/runbook.md`.

## Development

Python 3.11. Each service is a FastAPI app with its own `tests/` and
`requirements.txt`; shared code lives in `libs/common` (`sakura_common`).

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e libs/common
pip install -r services/ledger/requirements.txt   # per service you work on
python -m pytest libs/common/tests services/*/tests
```

## Documentation

- `docs/architecture.md` — services, data flow, and the reasoning behind them
- `docs/data-model.md` — every table of every service
- `docs/runbook.md` — operations: backup, restore, disaster recovery, upgrades
- `docs/ie6-style-guide.md` — the frontend rules and the retro visual style
- each `services/*/README.md` — service-level details; live OpenAPI docs at
  `/docs` on each service port
