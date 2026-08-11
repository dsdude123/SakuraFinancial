# Runbook

Operations guide for a SakuraFinancial deployment. Written for future-you,
two years from now, at 11pm, when something is weird.

## First start

```sh
docker compose up --build -d
docker compose ps       # wait until everything is healthy
```

There is nothing to configure first. Visit `http://<host>:8000`, set the login
password when prompted, then go to **Settings** for anything optional (LLM
provider + key if you want AI features; nothing is required).

The per-service databases are created by `infra/postgres-init/` **only on the
first start with an empty `pgdata` volume**, using the fixed passwords in the
`db` service's environment. Those are safe as constants because Postgres
publishes no port — it exists only on the stack's private network — while the
separate per-service users still stop any service from reading another's
tables. If you ever do change them in `docker-compose.yml`, change them in
Postgres too (`ALTER USER ... WITH PASSWORD ...`): the init script does not
re-run on an existing volume.

**The session-signing key is the one real secret.** web-ui generates a random
key on first boot and stores it at `/data/webui/session_secret` in the
`webui_data` volume; it is never a value shipped in this repository, because
a known key would let anyone forge a login cookie and skip the password.
Deleting that volume simply logs you out (a new key is generated). To manage
the key yourself, set `SECRET_KEY` in the environment and it wins.

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

Fresh host: check out the repo, `docker compose up --build -d`, wait healthy,
then Web UI → *Backup & Restore* → Upload your export zip. Nothing else to
restore first — the stack needs no configuration to start. Restore order
across services is handled by the restore endpoint itself (settings → ledger
→ budget → stocks → receipts). You'll set a login password again on the way
in unless the backup's settings export carries the old hash, which it does.

From a Postgres dump instead:
`cat sakura-YYYY-MM-DD.sql | docker compose exec -T db psql -U postgres`.

## Security posture

- Only web-ui is published (`:8000`), protected by a single-user password
  (hash stored in the settings service; session is a cookie signed with the
  per-installation key described above). Postgres and the five internal
  services have no published ports at all.
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

## Container images

CI publishes one image per service to GHCR
(`ghcr.io/<owner>/sakurafinancial-<service>`) on every push to `main` and on
`v*.*.*` tags; pull requests get a single mutable `pr-<n>` tag that is deleted
automatically when the PR closes. `docker-compose.ghcr.yml` runs the stack from
those images rather than building locally:

```sh
SAKURA_OWNER=<github-owner> SAKURA_TAG=v1.2.3 \
  docker compose -f docker-compose.ghcr.yml up -d
```

To cut a release, tag the commit: `git tag v1.2.3 && git push origin v1.2.3`.
The `latest` tag only moves for builds of the repository's **default branch**,
so make sure that is `main`.

If the cleanup workflow logs a 403 deleting a `pr-<n>` image, GitHub is
refusing `GITHUB_TOKEN` for a user-owned package: create a classic PAT with
`delete:packages` and save it as the `GHCR_CLEANUP_TOKEN` repository secret.

## Upgrades

`git pull && docker compose up --build -d` (or pull new images and
`docker compose -f docker-compose.ghcr.yml up -d`). Schema changes ship as SQLAlchemy
model changes applied on service startup for new tables; destructive
migrations will be called out in release notes with explicit steps. Take a
backup (both kinds) first, always.
