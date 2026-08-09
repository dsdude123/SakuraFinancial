"""ledger-service: the core of SakuraFinancial.

Owns money movement: accounts (incl. multi-currency cash and asset/liability
accounts), categories, payees + learned aliases, transactions with
multi-category splits, transfers, bills, and CSV import. Everything the
budget, receipts, and web-ui services show is derived from data held here.
"""

from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy import select

from .db import Base, make_engine, make_session_factory
from .models import Currency, DEFAULT_CURRENCIES
from .routers import (
    accounts,
    bills,
    categories,
    exports,
    imports,
    meta,
    payees,
    reports,
    transactions,
)


def create_app(database_url: str | None = None) -> FastAPI:
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)

    with session_factory() as db:
        if db.execute(select(Currency).limit(1)).scalar_one_or_none() is None:
            for code, name, decimals in DEFAULT_CURRENCIES:
                db.add(Currency(code=code, name=name, decimals=decimals))
            db.commit()

    app = FastAPI(title="SakuraFinancial ledger-service", version="1.0")
    app.state.session_factory = session_factory

    @app.get("/health")
    def health():
        return {"status": "ok"}

    app.include_router(meta.router)
    app.include_router(accounts.router)
    app.include_router(categories.router)
    app.include_router(payees.router)
    app.include_router(transactions.router)
    app.include_router(imports.router)
    app.include_router(bills.router)
    app.include_router(reports.router)
    app.include_router(exports.router)
    return app
