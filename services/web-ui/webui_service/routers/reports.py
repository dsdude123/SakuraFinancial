from __future__ import annotations

import datetime as dt
from decimal import Decimal

from fastapi import APIRouter, Depends, Request

from ..auth import require_login
from ..clients import ServiceError
from ..rendering import render
from .budget import month_nav

router = APIRouter(dependencies=[Depends(require_login)])


@router.get("/reports")
async def reports_home(request: Request):
    return render(request, "reports.html", {"month": dt.date.today().strftime("%Y-%m")})


@router.get("/reports/spending")
async def spending_report(request: Request, month: str | None = None):
    month = month or dt.date.today().strftime("%Y-%m")
    year, mon = (int(part) for part in month.split("-"))
    start = dt.date(year, mon, 1)
    end = (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
    actuals = await request.app.state.clients.ledger.get(
        "/api/reports/category-actuals",
        params={"start": start.isoformat(), "end": end.isoformat()},
    )
    expense_rows = [row for row in actuals if row["kind"] == "expense"]
    income_rows = [row for row in actuals if row["kind"] == "income"]
    total_spent = sum((-Decimal(row["net"]) for row in expense_rows), Decimal("0"))
    for row in expense_rows:
        spent = -Decimal(row["net"])
        row["spent"] = str(spent)
        row["pct"] = int(spent / total_spent * 100) if total_spent > 0 and spent > 0 else 0
    expense_rows.sort(key=lambda row: Decimal(row["spent"]), reverse=True)
    prev_month, next_month = month_nav(month)
    return render(
        request,
        "report_spending.html",
        {
            "month": month,
            "prev_month": prev_month,
            "next_month": next_month,
            "expense_rows": expense_rows,
            "income_rows": income_rows,
            "total_spent": str(total_spent),
        },
    )


@router.get("/reports/cashflow")
async def cashflow_report(request: Request, months: int = 12):
    series = await request.app.state.clients.ledger.get(
        "/api/reports/cashflow", params={"months": months}
    )
    return render(request, "report_cashflow.html", {"series": series, "months": months})


@router.get("/reports/networth")
async def networth_report(request: Request, months: int = 24):
    series = await request.app.state.clients.ledger.get(
        "/api/reports/net-worth", params={"months": months}
    )
    stocks_by_month: dict[str, str] = {}
    stocks_note = None
    try:
        stocks_series = await request.app.state.clients.stocks.get(
            "/api/valuation/series", params={"months": months}
        )
        stocks_by_month = {row["month"]: row["total"] for row in stocks_series}
    except ServiceError:
        stocks_note = "Stock accounts not included (stocks service unavailable)."
    for row in series:
        investments = Decimal(stocks_by_month.get(row["month"], "0"))
        row["investments"] = str(investments)
        row["grand_total"] = str(Decimal(row["total"]) + investments)
    missing = series[-1]["missing_rates"] if series else []
    return render(
        request,
        "report_networth.html",
        {"series": series, "months": months, "missing_rates": missing, "stocks_note": stocks_note},
    )
