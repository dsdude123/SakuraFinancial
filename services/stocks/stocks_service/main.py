"""stocks-service: investment accounts, daily price history, RSUs, analysis.

Account types: brokerage (trade freely), rsu (vesting schedules + trading
windows), managed (cash in/out only — performance tracking, no analysis).
Prices arrive once a day from Yahoo (previous close) via an in-process
scheduler, are kept forever, and can always be entered manually. AI analysis
is strictly on demand.
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI

from sakura_common.errors import install_error_handler
from sakura_common.schema import sync_schema
from sakura_common.settings_client import SettingsClient
from sakura_common.yahoo import YahooClient

from .db import Base, make_engine, make_session_factory
from .logic import prices as price_logic
from .routers import accounts, exports, imports, market

logger = logging.getLogger(__name__)

DEFAULT_FETCH_HOUR_UTC = 10  # ~2-3am Pacific: US markets long closed


def create_app(
    database_url: str | None = None,
    yahoo: YahooClient | None = None,
    settings_client: SettingsClient | None = None,
    enable_scheduler: bool | None = None,
) -> FastAPI:
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    sync_schema(engine, Base, service="stocks")
    session_factory = make_session_factory(engine)

    app = FastAPI(title="SakuraFinancial stocks-service", version="1.0")
    install_error_handler(app, "stocks")
    app.state.session_factory = session_factory
    app.state.yahoo = yahoo or YahooClient()
    app.state.settings_client = settings_client or SettingsClient()

    if enable_scheduler is None:
        enable_scheduler = os.environ.get("STOCKS_SCHEDULER", "1") == "1"

    if enable_scheduler:
        from apscheduler.schedulers.background import BackgroundScheduler

        def daily_price_job():
            with session_factory() as db:
                result = price_logic.refresh_all(db, app.state.yahoo)
                db.commit()
                logger.info("daily price fetch: %s", result)

        fetch_hour = DEFAULT_FETCH_HOUR_UTC
        configured = (app.state.settings_client.get("stocks.fetch_hour_utc") or "").strip()
        if configured.isdigit() and 0 <= int(configured) <= 23:
            fetch_hour = int(configured)
        scheduler = BackgroundScheduler(timezone="UTC")
        scheduler.add_job(daily_price_job, "cron", hour=fetch_hour, minute=10)
        scheduler.start()
        app.state.scheduler = scheduler
        logger.info("daily price fetch scheduled at %02d:10 UTC", fetch_hour)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    app.include_router(accounts.router)
    app.include_router(market.router)
    app.include_router(imports.router)
    app.include_router(exports.router)
    return app
