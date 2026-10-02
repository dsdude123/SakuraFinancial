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


class TestSequenceResync:
    """setval is not transactional. A reset or restore that rewinds sequences
    and then fails leaves the rows in place with their counters at 1, and the
    next insert dies on a duplicate primary key. Nothing recovers from that on
    its own, so boot checks and repairs it."""

    def test_sqlite_needs_no_resync(self):
        from ledger_service.db import Base
        from sakura_common.schema import resync_sequences

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        assert resync_sequences(engine, Base) == []

    def test_sync_schema_reports_both_repairs(self):
        from ledger_service.db import Base
        from sakura_common.schema import sync_schema

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        result = sync_schema(engine, Base, service="ledger")
        assert result == {
            "added_columns": [],
            "relaxed_columns": [],
            "repaired_sequences": [],
        }


def test_a_discarded_batch_does_not_block_the_next_import(client, checking):
    """The reported failure: discard a batch, import again, duplicate key.
    On SQLite this always passed; the guard is that ids keep climbing across
    batches rather than restarting."""
    profile = client.post(
        "/api/import/profiles",
        json={
            "name": "Bank",
            "account_id": checking["id"],
            "config": {
                "date_column": "date",
                "date_format": "%m/%d/%Y",
                "description_column": "description",
                "amount_column": "amount",
            },
        },
    ).json()

    def upload(content):
        return client.post(
            "/api/import/preview",
            json={"profile_id": profile["id"], "filename": "x.csv", "content": content},
        )

    first = upload(
        "Date,Description,Amount\n08/01/2026,ONE,-1.00\n08/02/2026,TWO,-2.00\n"
    ).json()
    client.post(f"/api/import/batches/{first['id']}/abort")

    second = upload(
        "Date,Description,Amount\n09/01/2026,THREE,-3.00\n09/02/2026,FOUR,-4.00\n"
    )
    assert second.status_code == 200
    batch = second.json()
    assert batch["total_rows"] == 2
    first_ids = {r["id"] for r in first["rows"]}
    second_ids = {r["id"] for r in batch["rows"]}
    assert not (first_ids & second_ids)


class TestRelaxingColumnsNoLongerRequired:
    """A column can stop being mandatory — transfer_rules.account_id did, once a
    rule could point at an investment account instead of a ledger one. On a
    database created before that change the column is still NOT NULL, and the
    first such rule would die on insert."""

    def relax(self, table_name, live_columns):
        from sakura_common.schema import columns_to_relax
        from ledger_service.db import Base

        return columns_to_relax(Base.metadata.tables[table_name], live_columns)

    def live(self, **nullability):
        return [
            {"name": name, "nullable": nullable} for name, nullable in nullability.items()
        ]

    def test_a_column_the_model_made_optional_is_found(self):
        assert self.relax("transfer_rules", self.live(account_id=False)) == ["account_id"]

    def test_one_already_optional_is_left_alone(self):
        assert self.relax("transfer_rules", self.live(account_id=True)) == []

    def test_a_column_still_required_by_the_model_is_left_alone(self):
        assert self.relax("transfer_rules", self.live(pattern=False)) == []

    def test_primary_keys_are_never_touched(self):
        assert self.relax("transfer_rules", self.live(id=False)) == []

    def test_a_column_the_database_does_not_have_yet_is_left_to_add_missing_columns(self):
        assert self.relax("transfer_rules", self.live()) == []

    def test_sqlite_needs_none_of_it(self):
        """A table create_all just made already matches the model, and SQLite
        cannot ALTER nullability anyway."""
        from sakura_common.schema import relax_nullable_columns
        from ledger_service.db import Base

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        assert relax_nullable_columns(engine, Base) == []
