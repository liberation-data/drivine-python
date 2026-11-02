"""
A connection to a SQLite file, as Drivine opens it: drivine4j's SqliteConnectionTest and SqliteLockWarningTest, ported.
"""

import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any

import pytest

from drivine import (
    ConnectionProperties,
    DatabaseRegistry,
    DatabaseType,
    DrivineError,
    NodeFragment,
    NodeId,
    PersistenceManager,
    QuerySpecification,
    StatelessGraphObjectManager,
    transactional,
    uniqueness_constraint,
)
from drivine.backend.sqlite.sqlite_graph import JSON_ROWS
from drivine.connection.sqlite import sqlite_session
from drivine.connection.sqlite.sqlite_connection_provider import SqliteConnectionProvider
from drivine.schema.schema_grammar import schema_grammar_for


class Gadget(NodeFragment, labels=["Gadget"]):
    id: Annotated[str, NodeId()]
    name: str | None = None


async def rows(pm: PersistenceManager, statement: str, **bindings: Any) -> list[Any]:
    return await pm.query(QuerySpecification[Any](statement).bind(bindings))


async def test_each_connection_writes_ahead_to_a_log_enforces_foreign_keys_and_waits_for_a_locked_file(path: str):
    registry = DatabaseRegistry()
    provider = SqliteConnectionProvider("sqlite", path, busy_timeout=1234)
    registry.register(provider)
    try:
        connection = await provider.connect()
        try:
            for pragma, expected in (("journal_mode", "wal"), ("foreign_keys", 1), ("busy_timeout", 1234)):
                [row] = await connection.query(QuerySpecification[Any](f"PRAGMA {pragma}"))
                assert next(iter(row.values())) == expected
        finally:
            await connection.release()
    finally:
        await registry.close()


async def test_every_connection_of_the_pool_is_opened_so_with_the_functions_registered(path: str):
    provider = SqliteConnectionProvider("sqlite", path)
    connections = [await provider.connect() for _ in range(3)]
    try:
        for connection in connections:
            [row] = await connection.query(
                QuerySpecification[Any]("SELECT drivine_epoch('1970-01-01T00:00:01.000000001Z') AS epoch")
            )
            assert row["epoch"] == 1_000_000_001
            [row] = await connection.query(QuerySpecification[Any]("PRAGMA foreign_keys"))
            assert row["foreign_keys"] == 1
    finally:
        for connection in connections:
            await connection.release()
        await provider.end()


async def test_a_value_is_read_as_what_it_is_and_text_a_statement_of_its_own_returns_stays_text(pm: PersistenceManager):
    [plain] = await rows(pm, "SELECT '{\"a\":1}' AS text, 7 AS number, 1.5 AS real, NULL AS missing")
    assert plain == {"text": '{"a":1}', "number": 7, "real": 1.5, "missing": None}
    # A statement Drivine writes opens with the marker, and its text values are JSON.
    [document] = await rows(pm, f"{JSON_ROWS}\nSELECT json_object('a', 1, 'b', json_array('x')) AS doc")
    assert document == {"doc": {"a": 1, "b": ["x"]}}


async def test_a_moment_a_date_a_decimal_and_a_map_are_bound_as_sqlite_keeps_them(pm: PersistenceManager):
    [row] = await rows(
        pm,
        "SELECT $moment AS moment, $day AS day, $amount AS amount, json_extract($map, '$.a') AS a",
        moment=datetime(2024, 5, 6, 7, 8, 9, 123000, tzinfo=UTC),
        day=date(2024, 5, 6),
        amount=Decimal("2.50"),
        map={"a": [1, 2]},
    )
    assert row == {"moment": "2024-05-06T07:08:09.123Z", "day": "2024-05-06", "amount": 2.5, "a": "[1,2]"}
    # A positional parameter is SQLite's ?1.
    assert await pm.query(QuerySpecification[Any]("SELECT $1 + $2 AS total").bind([2, 3])) == [{"total": 5}]


async def test_the_functions_drivines_statements_call_are_there(pm: PersistenceManager):
    [row] = await rows(
        pm,
        "SELECT drivine_lower('ÉCOLE') AS lowered, drivine_regexp_count('a b a', 'a') AS count, "
        "'abc' REGEXP '^a' AS matched, drivine_similarity('[1, 0]', '[1, 0]', 'COSINE') AS similarity",
    )
    assert row == {"lowered": "école", "count": 2, "matched": 1, "similarity": 1.0}


async def test_a_cypher_statement_is_refused_until_it_is_compiled_for_sqlite(pm: PersistenceManager):
    from drivine import cypher_statement

    with pytest.raises(DrivineError, match="#93"):
        await pm.query(QuerySpecification[Any](cypher_statement("MATCH (n) RETURN n")))


class Writes:
    def __init__(self, gom: StatelessGraphObjectManager) -> None:
        self.gom = gom

    @transactional()
    async def both(self) -> None:
        await self.gom.save(Gadget(id="g1"))
        await self.gom.save(Gadget(id="g2"))

    @transactional()
    async def then_fail(self) -> None:
        await self.gom.save(Gadget(id="g3"))
        raise ValueError("after the write")


async def test_a_transaction_that_rolls_back_writes_nothing_and_one_that_commits_writes_it_all(gom):
    await Writes(gom).both()
    with pytest.raises(ValueError, match="after the write"):
        await Writes(gom).then_fail()
    assert sorted(it.id for it in await gom.load_all(Gadget)) == ["g1", "g2"]


async def test_a_commit_sqlite_refuses_leaves_the_transaction_to_roll_back(path: str):
    provider = SqliteConnectionProvider("sqlite", path)
    try:
        connection = await provider.connect()
        for statement in (
            "CREATE TABLE parent (id INTEGER PRIMARY KEY)",
            "CREATE TABLE child (parent INTEGER REFERENCES parent (id) DEFERRABLE INITIALLY DEFERRED)",
        ):
            await connection.query(QuerySpecification[Any](statement))
        await connection.start_transaction()
        await connection.query(QuerySpecification[Any]("INSERT INTO child (parent) VALUES (1)"))
        with pytest.raises(Exception, match="FOREIGN KEY"):
            await connection.commit_transaction()
        await connection.rollback_transaction()
        await connection.release()
        # The connection went back to its pool with no transaction open.
        again = await provider.connect()
        assert await again.query(QuerySpecification[Any]("SELECT COUNT(*) AS n FROM child")) == [{"n": 0}]
        await again.start_transaction()
        await again.rollback_transaction()
        await again.release()
    finally:
        await provider.end()


async def test_a_transaction_sqlite_rolled_back_already_is_rolled_back_without_failing(path: str):
    provider = SqliteConnectionProvider("sqlite", path)
    try:
        connection = await provider.connect()
        await connection.start_transaction()
        await connection.query(QuerySpecification[Any]("ROLLBACK"))
        await connection.rollback_transaction()
        await connection.release()
    finally:
        await provider.end()


async def test_a_save_that_fails_after_its_first_steps_wrote_rolls_back_its_own_steps_and_says_why(
    gom: StatelessGraphObjectManager,
):
    from tests.models import Organization, Person, PersonCareer, WorkHistory

    assert (await gom.constraints.ensure(uniqueness_constraint("Organization", "name"))).status == "CREATED"
    await gom.save(Organization(uuid="o1", name="Taken"))
    career = PersonCareer(
        person=Person(uuid="p1", name="Ada"),
        employment_history=[
            WorkHistory(start_date="1843", role="analyst", target=Organization(uuid="o2", name="Taken"))
        ],
    )
    # The root was written before the constraint refused the related node: the save's own steps are undone.
    with pytest.raises(Exception, match="UNIQUE constraint failed"):
        await gom.save(career)
    assert await gom.load(Person, "p1") is None


async def test_a_provider_is_built_from_properties_with_the_files_path_as_the_database_name_and_no_host(path: str):
    properties = ConnectionProperties.from_env(
        "store",
        {"store_DATABASE_TYPE": "SQLITE", "store_DATABASE_NAME": path, "store_DATABASE_DEFAULT_GRAPH_PATH": "g"},
    )
    registry = DatabaseRegistry()
    provider = registry.builder().with_properties(properties).register("store")
    try:
        assert isinstance(provider, SqliteConnectionProvider)
        assert (provider.type, provider.path, provider.graph_name) == (DatabaseType.SQLITE, path, "g")
        connection = await provider.connect()
        tables = await connection.query(
            QuerySpecification[Any]("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
        )
        await connection.release()
        assert [it["name"] for it in tables] == ["g_edge", "g_node", "g_schema"]
    finally:
        await registry.close()
    with pytest.raises(DrivineError, match="DATABASE_NAME"):
        ConnectionProperties.from_env("store", {"store_DATABASE_TYPE": "SQLITE"})


def test_the_schema_grammar_of_a_sqlite_store_is_sqlites():
    assert schema_grammar_for(DatabaseType.SQLITE).engine == "SQLite"


async def test_a_transaction_held_past_the_warning_is_logged_as_it_ends_however_it_ends(
    path: str, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(sqlite_session, "LOCK_WARNING_SECONDS", 0.0)
    provider = SqliteConnectionProvider("sqlite", path)
    try:
        with caplog.at_level(logging.WARNING, "drivine.connection"):
            for end in ("commit", "rollback", "release"):
                connection = await provider.connect()
                await connection.start_transaction()
                if end == "commit":
                    await connection.commit_transaction()
                elif end == "rollback":
                    await connection.rollback_transaction()
                await connection.release()
            warnings = [it for it in caplog.records if "write lock" in it.getMessage()]
            assert len(warnings) == 3
            assert all("no other writer can write" in it.getMessage() for it in warnings)
    finally:
        await provider.end()


async def test_a_short_transaction_and_a_statement_outside_one_are_not_logged(
    path: str, caplog: pytest.LogCaptureFixture
):
    provider = SqliteConnectionProvider("sqlite", path)
    try:
        with caplog.at_level(logging.WARNING, "drivine.connection"):
            connection = await provider.connect()
            await connection.start_transaction()
            await connection.commit_transaction()
            await connection.query(QuerySpecification[Any]("SELECT 1"))
            await connection.release()
        assert not [it for it in caplog.records if "write lock" in it.getMessage()]
    finally:
        await provider.end()
