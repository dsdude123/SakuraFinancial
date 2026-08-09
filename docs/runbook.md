# Runbook

Operations guide for a SakuraFinancial deployment. Written for future-you,
two years from now, at 11pm, when something is weird.

## First start

```sh
cp .env.example .env    # set real passwords
docker compose up --build -d
docker compose ps       # wait until everything is healthy
```

Visit `http://<host>:8000`, set the login password when prompted, then go to
**Settings** and configure what you need (LLM provider + key if you want AI
features; nothing is required).

The per-service databases are created by `infra/postgres-init/` **only on the
first start with an empty `pgdata` volume**. If you change DB passwords in
`.env` later, you must also change them in Postgres (`ALTER USER ... WITH
PASSWORD ...`) — the init script will not re-run.

## Backups

Two complementary mechanisms; use both.

1. **Application export (human-readable, portable).** Web UI → *Backup &
   Restore* → Download. Produces a zip containing one JSON file per service
   plus the original receipt documents. This is the disaster-recovery format:
   it can be imported into a brand-new stack and is readable by a human (or a
   future system) without SakuraFinancial at all.
2. **Postgres volume snapshot (fast, exact).**
   `docker compose exec db pg_dumpall -U postgres > sakura-$(date +%F).sql`
   on a cron, copied off-machine.

The whole point of this platform is that the Windows 98 machine was a single
point of failure — schedule (2) nightly and do (1) after each monthly update
session, storing copies off the host.

## Restore / disaster recovery

Fresh host: check out the repo, restore `.env`, `docker compose up -d`, wait
healthy, then Web UI → *Backup & Restore* → Upload your export zip. Restore
order across services is handled by the restore endpoint itself (settings →
ledger → budget → stocks → receipts).

From a Postgres dump instead:
`cat sakura-YYYY-MM-DD.sql | docker compose exec -T db psql -U postgres`.

## Security posture

- Only web-ui is published (`:8000`), protected by a single-user password
  (hash stored in the settings service; session is a signed cookie).
- **Plain HTTP by design**: IE6 cannot do modern TLS. Keep the port on a
  trusted LAN; if you also browse from modern devices, put a TLS reverse
  proxy on a *different* published port and leave :8000 LAN-only.
- Secrets (API keys) live in the settings database, marked `is_secret` and
  masked in the UI; the export zip **does** contain them, so treat backups as
  sensitive.
- Nothing ever connects to a bank or brokerage. Outbound traffic is only:
  Yahoo Finance (stock quotes) and, if configured, your LLM provider.

## Scheduled jobs

- stocks-service fetches the previous close for all held symbols once a day
  (APScheduler inside the service; hour configurable via settings key
  `stocks.fetch_hour_utc`, default 10:00 UTC). Manual trigger: the *Refresh
  prices* button, or `POST /api/prices/refresh` on :8003 from inside the
  network.

## Troubleshooting

- **A service is unhealthy:** `docker compose logs <service>`. All services
  log to stdout.
- **Web UI says a service is unreachable:** it renders which one; check that
  container. The UI stays up even when a backend is down.
- **Stock prices stopped updating:** Yahoo occasionally changes/rate-limits
  endpoints. Check `docker compose logs stocks`; prices can always be entered
  manually meanwhile.
- **Import rejected my CSV:** that is by design — the error page lists each
  bad row and why. Fix the import profile (or the file) and re-upload;
  nothing was written.
- **Forgot the UI password:** delete the `ui.password_hash` row:
  `docker compose exec db psql -U sakura_settings -d sakura_settings -c
  "DELETE FROM settings WHERE key='ui.password_hash';"` — the next visit
  prompts you to set a new one.

## Upgrades

`git pull && docker compose up --build -d`. Schema changes ship as SQLAlchemy
model changes applied on service startup for new tables; destructive
migrations will be called out in release notes with explicit steps. Take a
backup (both kinds) first, always.
