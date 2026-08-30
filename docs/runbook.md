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

## Starting over (factory reset)

Web UI → *Settings* → *Reset everything*. This erases every record in every
service — accounts, transactions, bills, budgets, goals, investments,
receipts and their scanned files, and saved settings including API keys — and
leaves the stack running like a fresh install.

The flow will not let you reach the wipe without a backup in hand:

1. It builds the same zip as *Backup & Restore* and parks it on the web-ui
   volume. If any service is unreachable the backup would be incomplete, so
   the reset refuses to start.
2. You download it. The wipe stays locked until the zip has actually been
   sent to the browser — a backup you never received is not a backup.
3. You type `ERASE` and tick the box.

Two things deliberately survive: your **login password** (a reset clears your
data, it shouldn't lock you out of the machine) and the ledger's **default
currencies** (without them no account can be created and the "fresh install"
wouldn't be usable). The parked copy is deleted once the wipe finishes, or if
you cancel; abandoned ones are swept after six hours. Everything is
recoverable from the zip you just downloaded via *Backup & Restore*.

Each service also exposes `POST /api/reset` directly if you'd rather wipe one
of them — it takes no backup and asks no questions, so use the UI unless you
know exactly why you're not.

## Schema upgrades and self-repair

Every service runs `sakura_common.schema.sync_schema` at boot. It does two
things, both additive and both safe to repeat:

1. **`add_missing_columns`** issues `ALTER TABLE ... ADD COLUMN` for anything
   the models declare and the database lacks. `create_all` makes missing
   tables but never alters an existing one, so without this a newly shipped
   column leaves an upgraded install throwing `UndefinedColumn` on the first
   query. It only adds — never drops, renames or retypes.
2. **`resync_sequences`** drags any `id` sequence that has fallen behind its
   table back past `MAX(id)`. Postgres sequences are **not transactional**: a
   `setval` survives a rollback, so a restore or reset that rewinds the
   counters and then fails leaves the rows in place with their sequences at 1,
   and the next insert dies on a duplicate primary key. Sequences are only
   ever moved forward; one that is already ahead is left alone.

If the logs show `id sequences were behind their tables and have been
repaired`, that is this working — a restore or reset was interrupted at some
point, and the damage has been undone.

Anything beyond these (a real migration) is a restore-from-export job.

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

- **Something went wrong on a page:** the error page carries an error id and,
  when the failure was a genuine crash rather than a deliberate rejection, a
  **Show the technical details** button with the full stack trace. *Error log*
  in the nav lists the recent ones. That is the same trace the service logged,
  so you rarely need `docker compose logs` for an application error. The log is
  in memory only and clears when web-ui restarts.
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
