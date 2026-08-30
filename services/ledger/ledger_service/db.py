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


def add_missing_columns(engine, base=Base) -> list[str]:
    """Additive schema sync: ``ALTER TABLE ... ADD COLUMN`` for anything the
    models declare that the live database doesn't have yet.

    ``Base.metadata.create_all`` creates missing *tables* but never touches an
    existing one, so shipping a new column would leave upgraded installs
    throwing UndefinedColumn on the first query. There is no migration
    framework here on purpose (one database, one user, upgrades are a
    ``docker compose pull``), so this covers the case that actually comes up.
    It only ever adds nullable columns — never drops, renames, or retypes, so
    it cannot destroy data or need a rollback path.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    added: list[str] = []
    with engine.begin() as connection:
        for name, table in base.metadata.tables.items():
            if name not in existing_tables:
                continue  # create_all just made it, with every column
            present = {column["name"] for column in inspector.get_columns(name)}
            for column in table.columns:
                if column.name in present:
                    continue
                type_sql = column.type.compile(engine.dialect)
                clause = f'ALTER TABLE {name} ADD COLUMN "{column.name}" {type_sql}'
                default = column.default.arg if column.default is not None else None
                if default is not None and not callable(default):
                    literal = f"'{default}'" if isinstance(default, str) else str(default)
                    if isinstance(default, bool):
                        literal = "true" if default else "false"
                    clause += f" DEFAULT {literal}"
                connection.execute(text(clause))
                added.append(f"{name}.{column.name}")
    return added
