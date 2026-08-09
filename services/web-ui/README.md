# web-ui (:8000)

The only service a browser ever touches, and the only published port.
Server-side rendered Jinja2 → HTML 4.01, deliberately styled like a Win98-era
desktop app, and functional on IE6 without a single line of JavaScript. The
rules live in `docs/ie6-style-guide.md` and are enforced by
`tests/test_ie6_lint.py`.

## How it's built

- **No data of its own.** Every page is assembled from the service APIs at
  request time (`webui_service/clients.py`, async httpx). A dead backend
  renders a friendly error page naming the container to check.
- **Every interaction is a form POST → redirect → GET.** Refresh-safe,
  bookmark-safe, IE6-safe. Flash messages travel as query params.
- **No-JS interaction patterns:** payee auto-fill is a "Fill" submit that
  re-renders the entry form with the payee's defaults; "add split line"
  re-renders the split editor with one more row; confirmations are separate
  pages.
- **Auth:** single password, PBKDF2 hash stored in the settings service
  (`ui.password_hash`), signed session cookie (Starlette SessionMiddleware).
  First visit prompts to set the password. Reset procedure: docs/runbook.md.
- Charts are matplotlib PNGs rendered by this service (`/charts/...`) and
  embedded as `<img>`.

## Pages

Home (accounts + prompts + upcoming bills), per-account register with
auto-fill and running balance, transfers, asset valuation, transaction/split
editor, import wizard (validate → review → commit, with full-file error
reports), import profiles, bills overview (accrual + amount-change prompts),
budget grid + waterfall + General Fund, goals, payees (incl. learned CSV
aliases), categories, settings (AI provider, trading instructions,
currencies, FX rates, transfer rules, password), monthly updates, reports,
stocks, receipts, backup & restore.

## Tests

`tests/test_ie6_lint.py` — static lint of all templates + CSS against the
IE6 ruleset. `tests/test_webui_flows.py` — full-stack integration: the web
UI runs against REAL ledger/budget/settings apps booted in-process on SQLite
via httpx ASGI transports; flows tested end to end (first-run password,
register entry, import wizard incl. the all-or-nothing error report, bill
amount-review prompts, budget, settings).
