"""Additive schema sync. create_all makes missing tables but never alters an
existing one, so a newly shipped column would break upgraded installs on the
first query - which is exactly how the reset endpoint blew up in testing."""

from sqlalchemy import Column, Integer, String, create_engine, inspect, text

from ledger_service.db import add_missing_columns


def test_a_column_added_to_the_models_is_added_to_a_live_table():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name VARCHAR(20))"))
        connection.execute(text("INSERT INTO widgets (id, name) VALUES (1, 'existing row')"))

    from sqlalchemy.orm import DeclarativeBase

    class Base(DeclarativeBase):
        pass

    class Widget(Base):
        __tablename__ = "widgets"
        id = Column(Integer, primary_key=True)
        name = Column(String(20))
        match_days = Column(Integer, default=5)

    added = add_missing_columns(engine, base=Base)
    assert added == ["widgets.match_days"]
    columns = {c["name"] for c in inspect(engine).get_columns("widgets")}
    assert "match_days" in columns

    with engine.connect() as connection:
        row = connection.execute(text("SELECT name, match_days FROM widgets")).one()
    assert row[0] == "existing row"  # data survives
    assert row[1] == 5  # and the default is applied


def test_running_it_twice_changes_nothing():
    engine = create_engine("sqlite://")
    from sqlalchemy.orm import DeclarativeBase

    class Base(DeclarativeBase):
        pass

    class Widget(Base):
        __tablename__ = "widgets"
        id = Column(Integer, primary_key=True)
        note = Column(String(20))

    Base.metadata.create_all(engine)
    assert add_missing_columns(engine, base=Base) == []
    assert add_missing_columns(engine, base=Base) == []


def test_tables_that_do_not_exist_yet_are_left_to_create_all():
    engine = create_engine("sqlite://")
    from sqlalchemy.orm import DeclarativeBase

    class Base(DeclarativeBase):
        pass

    class Widget(Base):
        __tablename__ = "widgets"
        id = Column(Integer, primary_key=True)

    assert add_missing_columns(engine, base=Base) == []


def test_the_real_ledger_schema_is_already_in_sync():
    """A freshly created database needs no ALTERs at all."""
    from ledger_service.db import Base

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    assert add_missing_columns(engine) == []


def test_transfer_rules_gains_match_days_on_an_older_database():
    """The concrete upgrade this shipped for."""
    from ledger_service.db import Base

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE transfer_rules DROP COLUMN match_days"))
    assert "transfer_rules.match_days" in add_missing_columns(engine)
    columns = {c["name"] for c in inspect(engine).get_columns("transfer_rules")}
    assert "match_days" in columns
