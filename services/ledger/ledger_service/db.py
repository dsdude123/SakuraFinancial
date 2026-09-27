"""Database wiring. DATABASE_URL comes from the environment (docker-compose);
tests pass an in-memory SQLite URL to create_app instead."""

from __future__ import annotations

import os

from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str | None = None):
    url = database_url or os.environ.get("DATABASE_URL", "sqlite:///./ledger.sqlite3")
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    return create_engine(url, pool_pre_ping=True)


def make_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def get_db(request: Request):
    """FastAPI dependency: one session per request, from the app's factory."""
    db: Session = request.app.state.session_factory()
    try:
        yield db
    finally:
        db.close()


def add_missing_columns(engine, base=Base):
    """Kept as the ledger's entry point; the implementation is shared."""
    from sakura_common.schema import add_missing_columns as _add

    return _add(engine, base)
