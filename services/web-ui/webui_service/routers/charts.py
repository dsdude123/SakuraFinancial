"""Server-rendered chart PNGs.

IE6 can't run charting JavaScript, so charts are plain <img> tags pointing
here. Matplotlib (Agg backend) renders on the server with a deliberately
90s-financial-software palette; PNGs are opaque (no alpha) because IE6
renders alpha PNGs gray.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from io import BytesIO

from fastapi import APIRouter, Depends, Request, Response

import matplotlib

matplotlib.use("Agg")
from matplotlib.figure import Figure  # noqa: E402  (backend must be set first)

from ..auth import require_login

router = APIRouter(dependencies=[Depends(require_login)])

NAVY = "#000080"
PALETTE = [
    "#000080", "#008080", "#800000", "#808000", "#4b0082",
    "#a0522d", "#2e8b57", "#b22222", "#556b2f", "#483d8b",
    "#8b4513", "#c71585", "#191970", "#6b8e23",
]
GRID = "#c0c0c0"


def new_figure(width: float = 6.4, height: float = 3.6) -> Figure:
    figure = Figure(figsize=(width, height), dpi=100, facecolor="#ffffff")
    return figure


def to_png(figure: Figure) -> Response:
    buffer = BytesIO()
    figure.savefig(buffer, format="png", facecolor=figure.get_facecolor())
    return Response(buffer.getvalue(), media_type="image/png")


def style_axes(axes) -> None:
    axes.set_facecolor("#ffffff")
    for spine in axes.spines.values():
        spine.set_color(GRID)
    axes.tick_params(colors="#404040", labelsize=8)
    axes.yaxis.grid(True, color=GRID, linewidth=0.6)
    axes.set_axisbelow(True)


@router.get("/charts/spending.png")
async def spending_chart(request: Request, month: str):
    year, mon = (int(part) for part in month.split("-"))
    start = dt.date(year, mon, 1)
    end = (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
    actuals = await request.app.state.clients.ledger.get(
        "/api/reports/category-actuals",
        params={"start": start.isoformat(), "end": end.isoformat()},
    )
    slices = [
        (row["name"], -Decimal(row["net"]))
        for row in actuals
        if row["kind"] == "expense" and Decimal(row["net"]) < 0
    ]
    slices.sort(key=lambda item: item[1], reverse=True)
    figure = new_figure(6.0, 3.6)
    axes = figure.add_subplot()
    if slices:
        labels = [name for name, _ in slices]
        values = [float(value) for _, value in slices]
        wedges, _texts, _autotexts = axes.pie(
            values,
            colors=PALETTE[: len(values)] * (len(values) // len(PALETTE) + 1),
            autopct="%1.0f%%",
            textprops={"fontsize": 8, "color": "#ffffff"},
            wedgeprops={"edgecolor": "#ffffff", "linewidth": 1},
        )
        axes.legend(
            wedges, labels, loc="center left", bbox_to_anchor=(1.02, 0.5), fontsize=8, frameon=False
        )
    else:
        axes.text(0.5, 0.5, "No spending this month", ha="center", fontsize=10, color="#808080")
        axes.set_axis_off()
    figure.suptitle(f"Spending by category — {month}", fontsize=10, color=NAVY)
    figure.subplots_adjust(left=0.02, right=0.62)
    return to_png(figure)


@router.get("/charts/cashflow.png")
async def cashflow_chart(request: Request, months: int = 12):
    series = await request.app.state.clients.ledger.get(
        "/api/reports/cashflow", params={"months": months}
    )
    labels = [row["month"] for row in series]
    income = [float(row["income"]) for row in series]
    spending = [float(row["spending"]) for row in series]
    figure = new_figure(7.2, 3.4)
    axes = figure.add_subplot()
    style_axes(axes)
    positions = range(len(labels))
    axes.bar([p - 0.2 for p in positions], income, width=0.38, color="#2e8b57", label="Income")
    axes.bar([p + 0.2 for p in positions], spending, width=0.38, color="#b22222", label="Spending")
    axes.set_xticks(list(positions))
    axes.set_xticklabels(labels, rotation=45, ha="right", fontsize=7)
    axes.legend(fontsize=8, frameon=False)
    figure.suptitle("Cash flow (cash accounts only)", fontsize=10, color=NAVY)
    figure.subplots_adjust(bottom=0.28)
    return to_png(figure)


@router.get("/charts/networth.png")
async def networth_chart(request: Request, months: int = 24):
    series = await request.app.state.clients.ledger.get(
        "/api/reports/net-worth", params={"months": months}
    )
    stocks_by_month = {}
    try:
        stocks_series = await request.app.state.clients.stocks.get(
            "/api/valuation/series", params={"months": months}
        )
        stocks_by_month = {row["month"]: float(row["total"]) for row in stocks_series}
    except Exception:  # stocks service optional; chart must render without it
        pass
    labels = [row["month"] for row in series]
    cash = [float(row["cash"]) for row in series]
    assets = [float(row["assets"]) for row in series]
    liabilities = [float(row["liabilities"]) for row in series]
    investments = [stocks_by_month.get(row["month"], 0.0) for row in series]
    total = [c + a + l + i for c, a, l, i in zip(cash, assets, liabilities, investments)]

    figure = new_figure(7.2, 3.4)
    axes = figure.add_subplot()
    style_axes(axes)
    axes.plot(labels, cash, color="#2e8b57", linewidth=1.5, label="Cash")
    axes.plot(labels, assets, color="#808000", linewidth=1.5, label="Assets")
    if any(investments):
        axes.plot(labels, investments, color="#008080", linewidth=1.5, label="Investments")
    if any(liabilities):
        axes.plot(labels, liabilities, color="#b22222", linewidth=1.5, label="Liabilities")
    axes.plot(labels, total, color=NAVY, linewidth=2.5, label="Net worth")
    axes.set_xticks(range(len(labels)))
    axes.set_xticklabels(labels, rotation=45, ha="right", fontsize=7)
    axes.legend(fontsize=8, frameon=False)
    figure.suptitle("Net worth — cash vs non-cash", fontsize=10, color=NAVY)
    figure.subplots_adjust(bottom=0.28)
    return to_png(figure)
