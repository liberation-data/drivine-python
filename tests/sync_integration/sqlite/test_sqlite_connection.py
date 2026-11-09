# Written by scripts/unasync.py from tests/integration/sqlite/test_sqlite_connection.py:
# change that, and run the script again.
"""
A connection to a SQLite file, as Drivine opens it: drivine4j's SqliteConnectionTest and SqliteLockWarningTest, ported.
"""

import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any

import pytest

from drivine.backend.sqlite.sqlite_graph import JSON_ROWS
from drivine.connection.sqlite import sqlite_session
from drivine.cursor.cursor_specification import CursorSpecification
from drivine.schema.schema_grammar import schema_grammar_for
from drivine.sync import (
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
from drivine.sync.connection.sqlite.sqlite_connection_provider import SqliteConnectionProvider


class Gadget(NodeFragment, labels=["Gadget"]):
    id: Annotated[str, NodeId()]
    name: str | None = None


def rows(pm: PersistenceManager, statement: str, **bindings: Any) -> list[Any]:
    return pm.query(QuerySpecification[Any](statement).bind(bindings))


def test_each_connection_writes_ahead_to_a_log_enforces_foreign_keys_and_waits_for_a_locked_file(path: str):
    registry = DatabaseRegistry()
    provider = SqliteConnectionProvider("sqlite", path, busy_timeout=1234)
    registry.register(provider)
    try:
        connection = provider.connect()
        try:
            for pragma, expected in (("journal_mode", "wal"), ("foreign_keys", 1), ("busy_timeout", 1234)):
                [row] = connection.query(QuerySpecification[Any](f"PRAGMA {pragma}"))
                assert next(iter(row.values())) == expected
        finally:
            connection.release()
    finally:
        registry.close()


def test_every_connection_of_the_pool_is_opened_so_with_the_functions_registered(path: str):
    provider = SqliteConnectionProvider("sqlite", path)
    connections = [provider.connect() for _ in range(3)]
    try:
        for connection in connections:
            [row] = connection.query(
                QuerySpecification[Any]("SELECT drivine_epoch('1970-01-01T00:00:01.000000001Z') AS epoch")
            )
            assert row["epoch"] == 1_000_000_001
            [row] = connection.query(QuerySpecification[Any]("PRAGMA foreign_keys"))
            assert row["foreign_keys"] == 1
    finally:
        for connection in connections:
            connection.release()
        provider.end()


def test_a_value_is_read_as_what_it_is_and_text_a_statement_of_its_own_returns_stays_text(pm: PersistenceManager):
    [plain] = rows(pm, "SELECT '{\"a\":1}' AS text, 7 AS number, 1.5 AS real, NULL AS missing")
    assert plain == {"text": '{"a":1}', "number": 7, "real": 1.5, "missing": None}
    # A statement Drivine writes opens with the marker, and its text values are JSON.
    [document] = rows(pm, f"{JSON_ROWS}\nSELECT json_object('a', 1, 'b', json_array('x')) AS doc")
    assert document == {"doc": {"a": 1, "b": ["x"]}}


def test_a_moment_a_date_a_decimal_and_a_map_are_bound_as_sqlite_keeps_them(pm: PersistenceManager):
    [row] = rows(
        pm,
        "SELECT $moment AS moment, $day AS day, $amount AS amount, json_extract($map, '$.a') AS a",
        moment=datetime(2024, 5, 6, 7, 8, 9, 123000, tzinfo=UTC),
        day=date(2024, 5, 6),
        amount=Decimal("2.50"),
        map={"a": [1, 2]},
    )
    assert row == {"moment": "2024-05-06T07:08:09.123Z", "day": "2024-05-06", "amount": 2.5, "a": "[1,2]"}
    # A positional parameter is SQLite's ?1.
    assert pm.query(QuerySpecification[Any]("SELECT $1 + $2 AS total").bind([2, 3])) == [{"total": 5}]


def test_the_functions_drivines_statements_call_are_there(pm: PersistenceManager):
    [row] = rows(
        pm,
        "SELECT drivine_lower('ÉCOLE') AS lowered, drivine_regexp_count('a b a', 'a') AS count, "
        "'abc' REGEXP '^a' AS matched, drivine_similarity('[1, 0]', '[1, 0]', 'COSINE') AS similarity",
    )
    assert row == {"lowered": "école", "count": 2, "matched": 1, "similarity": 1.0}


def test_a_cypher_statement_is_compiled_to_sqlites_sql_and_gives_a_graph_engines_rows(pm: PersistenceManager):
    from drivine.sync import cypher_statement

    written = (
        "CREATE (a:CyConn {name: 'Ada', born: 1815})-[:KNOWS {since: 1833}]->(b:CyConn {name: 'Charles'}) RETURN a.name"
    )
    assert pm.query(QuerySpecification[Any](cypher_statement(written))) == ["Ada"]
    read = "MATCH (a:CyConn)-[k:KNOWS]->(b) RETURN {from: a.name, to: b.name, since: k.since, born: a.born / 1.0}"
    assert pm.query(QuerySpecification[Any](cypher_statement(read))) == [
        {"from": "Ada", "to": "Charles", "since": 1833, "born": 1815.0}
    ]
    optional = "MATCH (n:CyConn) OPTIONAL MATCH (n)-[:KNOWS]->(m) RETURN [n.name, m.name] ORDER BY n.name"
    assert pm.query(QuerySpecification[Any](cypher_statement(optional))) == [
        ["Ada", "Charles"],
        ["Charles", None],
    ]


class Writes:
    def __init__(self, gom: StatelessGraphObjectManager) -> None:
        self.gom = gom

    @transactional()
    def both(self) -> None:
        self.gom.save(Gadget(id="g1"))
        self.gom.save(Gadget(id="g2"))

    @transactional()
    def then_fail(self) -> None:
        self.gom.save(Gadget(id="g3"))
        raise ValueError("after the write")


def test_a_transaction_that_rolls_back_writes_nothing_and_one_that_commits_writes_it_all(gom):
    Writes(gom).both()
    with pytest.raises(ValueError, match="after the write"):
        Writes(gom).then_fail()
    assert sorted(it.id for it in gom.load_all(Gadget)) == ["g1", "g2"]


def test_a_commit_sqlite_refuses_leaves_the_transaction_to_roll_back(path: str):
    provider = SqliteConnectionProvider("sqlite", path)
    try:
        connection = provider.connect()
        for statement in (
            "CREATE TABLE parent (id INTEGER PRIMARY KEY)",
            "CREATE TABLE child (parent INTEGER REFERENCES parent (id) DEFERRABLE INITIALLY DEFERRED)",
        ):
            connection.query(QuerySpecification[Any](statement))
        connection.start_transaction()
        connection.query(QuerySpecification[Any]("INSERT INTO child (parent) VALUES (1)"))
        with pytest.raises(Exception, match="FOREIGN KEY"):
            connection.commit_transaction()
        connection.rollback_transaction()
        connection.release()
        # The connection went back to its pool with no transaction open.
        again = provider.connect()
        assert again.query(QuerySpecification[Any]("SELECT COUNT(*) AS n FROM child")) == [{"n": 0}]
        again.start_transaction()
        again.rollback_transaction()
        again.release()
    finally:
        provider.end()


def test_a_transaction_whose_commit_sqlite_refuses_says_why_and_gives_its_connection_back_rolled_back(
    pm: PersistenceManager,
):
    rows(pm, "CREATE TABLE parent (id INTEGER PRIMARY KEY)")
    rows(pm, "CREATE TABLE child (parent INTEGER REFERENCES parent (id) DEFERRABLE INITIALLY DEFERRED)")

    @transactional()
    def orphan() -> None:
        rows(pm, "INSERT INTO child (parent) VALUES (1)")

    @transactional()
    def adopted() -> None:
        rows(pm, "INSERT INTO parent (id) VALUES (1)")
        rows(pm, "INSERT INTO child (parent) VALUES (1)")

    with pytest.raises(DrivineError, match="Committing") as refused:
        orphan()
    assert "FOREIGN KEY" in str(refused.value.__cause__)
    # The connection went back to its pool with the transaction rolled back, and the next one commits on it.
    assert rows(pm, "SELECT COUNT(*) AS n FROM child") == [{"n": 0}]
    adopted()
    assert rows(pm, "SELECT COUNT(*) AS n FROM child") == [{"n": 1}]


def test_a_connection_given_back_after_its_pool_ended_is_closed_and_not_kept(path: str):
    provider = SqliteConnectionProvider("sqlite", path)
    connection = provider.connect()
    provider.end()
    connection.release()
    with pytest.raises(Exception, match="closed"):
        connection.session.connection.execute("SELECT 1")
    again = provider.connect()
    try:
        assert again.session is not connection.session
    finally:
        again.release()
        provider.end()


def test_a_transaction_sqlite_rolled_back_already_is_rolled_back_without_failing(path: str):
    provider = SqliteConnectionProvider("sqlite", path)
    try:
        connection = provider.connect()
        connection.start_transaction()
        connection.query(QuerySpecification[Any]("ROLLBACK"))
        connection.rollback_transaction()
        connection.release()
    finally:
        provider.end()


def test_a_save_that_fails_after_its_first_steps_wrote_rolls_back_its_own_steps_and_says_why(
    gom: StatelessGraphObjectManager,
):
    from tests.models import Organization, Person, PersonCareer, WorkHistory

    assert (gom.constraints.ensure(uniqueness_constraint("Organization", "name"))).status == "CREATED"
    gom.save(Organization(uuid="o1", name="Taken"))
    career = PersonCareer(
        person=Person(uuid="p1", name="Ada"),
        employment_history=[
            WorkHistory(start_date="1843", role="analyst", target=Organization(uuid="o2", name="Taken"))
        ],
    )
    # The root was written before the constraint refused the related node: the save's own steps are undone.
    with pytest.raises(Exception, match="UNIQUE constraint failed"):
        gom.save(career)
    assert gom.load(Person, "p1") is None


def test_a_provider_is_built_from_properties_with_the_files_path_as_the_database_name_and_no_host(path: str):
    properties = ConnectionProperties.from_env(
        "store",
        {"store_DATABASE_TYPE": "SQLITE", "store_DATABASE_NAME": path, "store_DATABASE_DEFAULT_GRAPH_PATH": "g"},
    )
    registry = DatabaseRegistry()
    provider = registry.builder().with_properties(properties).register("store")
    try:
        assert isinstance(provider, SqliteConnectionProvider)
        assert (provider.type, provider.path, provider.graph_name) == (DatabaseType.SQLITE, path, "g")
        connection = provider.connect()
        tables = connection.query(
            QuerySpecification[Any]("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
        )
        connection.release()
        # sqlite_sequence is SQLite's own, kept for the AUTOINCREMENT keys (drivine4j#713).
        assert [it["name"] for it in tables] == ["g_edge", "g_node", "g_schema", "sqlite_sequence"]
    finally:
        registry.close()
    with pytest.raises(DrivineError, match="DATABASE_NAME"):
        ConnectionProperties.from_env("store", {"store_DATABASE_TYPE": "SQLITE"})


def test_the_schema_grammar_of_a_sqlite_store_is_sqlites():
    assert schema_grammar_for(DatabaseType.SQLITE).engine == "SQLite"


def test_a_transaction_held_past_the_warning_is_logged_as_it_ends_however_it_ends(
    path: str, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(sqlite_session, "LOCK_WARNING_SECONDS", 0.0)
    provider = SqliteConnectionProvider("sqlite", path)
    try:
        with caplog.at_level(logging.WARNING, "drivine.connection"):
            for end in ("commit", "rollback", "release"):
                connection = provider.connect()
                connection.start_transaction()
                if end == "commit":
                    connection.commit_transaction()
                elif end == "rollback":
                    connection.rollback_transaction()
                connection.release()
            warnings = [it for it in caplog.records if "write lock" in it.getMessage()]
            assert len(warnings) == 3
            assert all("no other writer can write" in it.getMessage() for it in warnings)
    finally:
        provider.end()


def test_a_short_transaction_and_a_statement_outside_one_are_not_logged(path: str, caplog: pytest.LogCaptureFixture):
    provider = SqliteConnectionProvider("sqlite", path)
    try:
        with caplog.at_level(logging.WARNING, "drivine.connection"):
            connection = provider.connect()
            connection.start_transaction()
            connection.commit_transaction()
            connection.query(QuerySpecification[Any]("SELECT 1"))
            connection.release()
        assert not [it for it in caplog.records if "write lock" in it.getMessage()]
    finally:
        provider.end()


def test_a_cursor_left_unread_is_closed_as_its_connection_is_released_so_its_next_user_reads_what_is_committed(
    path: str,
):
    provider = SqliteConnectionProvider("sqlite", path, pool_max=1)
    try:
        connection = provider.connect()
        connection.query(QuerySpecification[Any]("CREATE TABLE t (v INTEGER)"))
        connection.query(QuerySpecification[Any]("INSERT INTO t VALUES (1), (2), (3)"))
        cursor = connection.open_cursor(CursorSpecification[Any]("SELECT v FROM t").batch_size(1))
        cursor.read(1)
        connection.release()
        cursor.close()  # closed already, as the connection was released: closing it again is nothing
        other = SqliteConnectionProvider("other", path)
        try:
            writer = other.connect()
            writer.query(QuerySpecification[Any]("INSERT INTO t VALUES (4)"))
            writer.release()
        finally:
            other.end()
        again = provider.connect()
        assert again.session is connection.session
        assert again.query(QuerySpecification[Any]("SELECT COUNT(*) AS n FROM t")) == [{"n": 4}]
        again.release()
    finally:
        provider.end()
