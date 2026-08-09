"""Database wiring. DATABASE_URL comes from the environment (docker-compose);
tests pass an in-memory SQLite URL to create_app instead."""

from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str | None = None):
    url = database_url or os.environ.get("DATABASE_URL", "sqlite:///./settings.sqlite3")
    if url.startswith("sqlite"):
        # StaticPool + shared connection so an in-memory DB survives across
        # requests in tests.
        return create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    return create_engine(url, pool_pre_ping=True)


def make_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)
