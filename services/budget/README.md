# budget-service (:8002)

Envelope budgeting in the YNAB spirit, minus its three annoyances. All
actuals are derived live from ledger-service — the budget can never disagree
with the register. Live OpenAPI docs at `/docs`.

## The engine (`budget_service/logic.py`)

1. **Income assigns itself** — no "assign every dollar" chores. Each month's
   income flows through the waterfall:
   1. cover spending already made,
   2. set aside accruals for upcoming non-monthly bills (from ledger's
      `/api/bills/accrual`; a bill due *this* month is already tier 1),
   3. fund savings goals (priority order, capped at each goal's target),
   4. remainder → the **General Fund**. Unassigned money is fine and is never
      nagged about.
2. **Deficits carry, surpluses don't.** Overspending a category makes its
   `available` negative and the deficit carries into following months until
   healed by underspending. Underspending sends the leftover to the General
   Fund instead of padding next month's category.
3. A month where total outgo exceeds income drains the General Fund; a
   negative General Fund raises the `global_deficit` flag (the UI banner).

History recomputes from the first budgeted month on every request —
deterministic, nothing stored that can go stale. Reimbursements (inflows on
expense categories) reduce `spent` automatically because ledger's
category-actuals are net.

## API

| Endpoint | Purpose |
| -------- | ------- |
| `GET /api/budget/{yyyy-mm}` | Full month view: per-category budgeted / carry-in / spent / available, waterfall breakdown, General Fund, deficit flags |
| `PUT /api/budget/{yyyy-mm}/categories/{id}` | Set a category's planned amount (`0` clears it) |
| `POST /api/budget/{yyyy-mm}/copy-from-previous` | Start a month from last month's plan |
| `GET/POST/PUT/DELETE /api/goals…` | Savings goals (target, monthly pace, priority) |
| `GET /api/export` / `POST /api/import` | Backup / restore |
| `GET /health` | Liveness |

Returns 503 with a clear message when ledger-service is unreachable.
