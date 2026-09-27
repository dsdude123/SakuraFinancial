"""Boot-time schema repair: additive column sync, NOT NULL relaxation, and
sequence resync.

Services call ``Base.metadata.create_all`` on start, which creates missing
tables but never touches an existing one. These helpers cover the gaps that
actually bite a running install, and all are safe to run every boot: they only
ever add, widen, or move forward — never drop, rename, retype, tighten, or
rewind.
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text

logger = logging.getLogger(__name__)


def add_missing_columns(engine, base) -> list[str]:
    """``ALTER TABLE ... ADD COLUMN`` for anything the models declare that the
    live database lacks.

    Without this, shipping a new column leaves upgraded installs throwing
    UndefinedColumn on the first query that selects it."""
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
                    if isinstance(default, bool):
                        literal = "true" if default else "false"
                    elif isinstance(default, str):
                        literal = f"'{default}'"
                    else:
                        literal = str(default)
                    clause += f" DEFAULT {literal}"
                connection.execute(text(clause))
                added.append(f"{name}.{column.name}")
    return added


def relax_nullable_columns(engine, base) -> list[str]:
    """``ALTER TABLE ... ALTER COLUMN ... DROP NOT NULL`` where the models now
    say a column is optional but the live table still demands a value.

    The case this shipped for: ``transfer_rules.account_id`` was required while
    the only counterparty a rule could name was another ledger account. Now a
    rule can point at an investment account instead, and the column has to be
    allowed to sit empty — on databases created before the change too, or the
    first such rule dies on a not-null violation.

    Only ever *widens*: this never adds a NOT NULL, which would fail outright on
    a table that already holds empty values. Primary keys are left alone
    whatever the model says.
    """
    if engine.dialect.name != "postgresql":
        # SQLite can't ALTER a column's nullability, and a table create_all
        # just made already matches the model.
        return []
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    relaxed: list[str] = []
    with engine.begin() as connection:
        for name, table in base.metadata.tables.items():
            if name not in existing_tables:
                continue
            for column_name in columns_to_relax(table, inspector.get_columns(name)):
                connection.execute(
                    text(f'ALTER TABLE {name} ALTER COLUMN "{column_name}" DROP NOT NULL')
                )
                relaxed.append(f"{name}.{column_name}")
    return relaxed


def columns_to_relax(table, live_columns) -> list[str]:
    """Which of ``table``'s columns the database still demands a value for
    although the model no longer does. ``live_columns`` is what the inspector
    reports for the table."""
    live = {column["name"]: column for column in live_columns}
    names = []
    for column in table.columns:
        if column.primary_key or not column.nullable:
            continue
        current = live.get(column.name)
        if current is None or current.get("nullable", True):
            continue
        names.append(column.name)
    return names


def resync_sequences(engine, base) -> list[str]:
    """Drag any id sequence that has fallen behind its table back past MAX(id).

    Sequences are **not transactional**: a ``setval`` survives a rollback. A
    restore or reset that sets sequences to 1 and then fails leaves the rows in
    place with their counters rewound, and the next insert dies on a duplicate
    primary key. Nothing in the app can recover from that on its own, so every
    boot checks and repairs it.

    Only ever moves a sequence *forward*. A counter that is already ahead
    (deleted rows, a gap) is left alone — lowering it would manufacture the
    very collision this exists to prevent.
    """
    if engine.dialect.name != "postgresql":
        return []  # SQLite's implicit rowid needs none of this
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    repaired: list[str] = []
    with engine.begin() as connection:
        for name, table in base.metadata.tables.items():
            if name not in existing_tables or "id" not in table.columns:
                continue
            sequence = connection.execute(
                text("SELECT pg_get_serial_sequence(:table, 'id')"), {"table": name}
            ).scalar_one_or_none()
            if not sequence:
                continue  # not a serial; nothing to keep in step
            highest = connection.execute(
                text(f"SELECT COALESCE(MAX(id), 0) FROM {name}")  # noqa: S608 - table from metadata
            ).scalar_one()
            next_value = connection.execute(
                text("SELECT last_value, is_called FROM " + sequence)
            ).one()
            last_value, is_called = next_value
            upcoming = last_value + 1 if is_called else last_value
            if upcoming > highest:
                continue  # already ahead of the data
            connection.execute(
                text("SELECT setval(:sequence, :value, false)"),
                {"sequence": sequence, "value": highest + 1},
            )
            repaired.append(f"{name}.id: {upcoming} -> {highest + 1}")
    return repaired


def sync_schema(engine, base, service: str = "") -> dict[str, list[str]]:
    """Every repair, logged. Call once at start-up after ``create_all``."""
    added = add_missing_columns(engine, base)
    relaxed = relax_nullable_columns(engine, base)
    repaired = resync_sequences(engine, base)
    if added:
        logger.info("%s: added columns %s", service or "schema", ", ".join(added))
    if relaxed:
        logger.info(
            "%s: columns no longer required, relaxed %s", service or "schema", ", ".join(relaxed)
        )
    if repaired:
        logger.warning(
            "%s: id sequences were behind their tables and have been repaired (%s). "
            "This normally means a restore or reset was interrupted.",
            service or "schema",
            "; ".join(repaired),
        )
    return {
        "added_columns": added,
        "relaxed_columns": relaxed,
        "repaired_sequences": repaired,
    }
