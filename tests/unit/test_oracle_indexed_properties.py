"""
The labelled indexes a Cypher statement run through an Oracle connection is compiled knowing, read with the shared
module's query of the graph's catalog and kept for the database, its user and the graph (#508): drivine4j's
OracleIndexedPropertiesTest (liberation-data/drivine4j#805). No database is needed: the catalog's rows are given as the
connection's cursor gives them.
"""

import json
import logging
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from drivine import QuerySpecification, cypher_statement
from drivine.backend.oracle import oracle_indexed_properties as indexed
from drivine.connection.oracle import oracle_connection
from drivine.connection.oracle.oracle_connection import OracleConnection
from drivine.cypher.shared_compiler import compile_cypher
from drivine.sync.connection.oracle import oracle_connection as sync_oracle_connection
from drivine.sync.connection.oracle.oracle_connection import OracleConnection as SyncOracleConnection

LABELLED = "SELECT labelled indexes"
"""The catalog's query, as these tests give it."""


@pytest.fixture
def graph() -> str:
    """A graph of its own for each test: what is read is kept for the process."""
    return f"oip_{uuid.uuid4().hex[:12]}"


@pytest.fixture
def now(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[float]]:
    """The clock, which a test moves on by adding to its one item."""
    moment = [0.0]
    monkeypatch.setattr(indexed, "clock", lambda: moment[0])
    yield moment


def row(label: str, *properties: str, kind: str = "RANGE_INDEX") -> dict[str, Any]:
    return {"kind": kind, "label": label, "properties": list(properties)}


class Catalog:
    """Rows that count each read, and give `rows` or raise `failing`."""

    def __init__(self, rows: list[Any], failing: BaseException | None = None) -> None:
        self.rows = rows
        self.failing = failing
        self.reads = 0

    def leading(self, database: str, graph: str) -> indexed.IndexedProperties:
        """What a connection to `database` compiles a statement of `graph` knowing, as the connection reads it."""
        key = indexed.key(database, graph)
        if key is None:
            return indexed.NONE
        known = indexed.known(key, graph)
        if known is not None:
            return known
        since = indexed.changes(graph)
        self.reads += 1
        if self.failing is not None:
            return indexed.failed(key, graph, self.failing, since)
        return indexed.found(key, graph, self.rows, since)


DB = "db|SCOTT"


def test_each_labels_indexes_give_the_property_each_begins_with_a_uniqueness_constraints_among_them(
    graph: str, now: list[float]
):
    catalog = Catalog(
        [
            row("Person", "name", "age"),
            row("Person", "id", kind="UNIQUENESS_CONSTRAINT"),
            json.dumps(row("City", "name")),
        ]
    )
    assert catalog.leading(DB, graph) == {("Person", "name"), ("Person", "id"), ("City", "name")}


def test_what_was_read_is_kept_until_it_is_a_minute_old_then_read_again(graph: str, now: list[float]):
    catalog = Catalog([row("Person", "name")])
    catalog.leading(DB, graph)
    catalog.rows = [row("Person", "name"), row("Person", "age")]
    now[0] += indexed.MAX_AGE - 0.001
    assert catalog.leading(DB, graph) == {("Person", "name")}
    assert catalog.reads == 1
    now[0] += 0.001
    assert catalog.leading(DB, graph) == {("Person", "name"), ("Person", "age")}
    assert catalog.reads == 2


def test_an_index_made_or_dropped_in_this_process_has_the_catalog_read_again_at_once_whatever_the_graph_names_case(
    graph: str, now: list[float]
):
    catalog = Catalog([row("Person", "name")])
    catalog.leading(DB, graph)
    catalog.rows = []
    indexed.changed(graph.upper())
    assert catalog.leading(DB, graph) == set()
    assert catalog.reads == 2
    assert catalog.leading(DB, graph.upper()) == set()
    assert catalog.reads == 2, "The same graph, named in another case, is the same store"


def test_an_index_made_while_the_catalog_is_read_has_it_read_again(graph: str, now: list[float]):
    key = indexed.key(DB, graph)
    assert key is not None
    since = indexed.changes(graph)
    indexed.changed(graph)
    indexed.found(key, graph, [row("Person", "name")], since)
    assert indexed.known(key, graph) is None


def test_each_database_and_user_has_its_own(graph: str, now: list[float]):
    catalog = Catalog([row("Person", "name")])
    catalog.leading(DB, graph)
    catalog.rows = [row("City", "name")]
    assert catalog.leading("db|OTHER", graph) == {("City", "name")}
    assert catalog.leading("other|SCOTT", graph) == {("City", "name")}
    assert catalog.leading(DB, graph) == {("Person", "name")}
    assert catalog.reads == 3


def test_a_catalog_that_cant_be_read_keeps_what_was_read_before_says_so_once_and_is_read_again_after_a_minute(
    graph: str, now: list[float], caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.DEBUG, "drivine.connection")
    catalog = Catalog([row("Person", "name")])
    catalog.leading(DB, graph)
    catalog.failing = RuntimeError("ORA-00942: table or view does not exist")
    now[0] += indexed.MAX_AGE
    assert catalog.leading(DB, graph) == {("Person", "name")}
    assert catalog.leading(DB, graph) == {("Person", "name")}
    assert catalog.reads == 2, "A read that failed is not tried again on every statement"
    now[0] += indexed.MAX_AGE
    assert catalog.leading(DB, graph) == {("Person", "name")}
    assert catalog.reads == 3
    warnings = [it for it in caplog.records if it.levelno == logging.WARNING]
    assert len(warnings) == 1 and graph in warnings[0].getMessage(), caplog.text
    catalog.failing = None
    catalog.rows = [row("City", "name")]
    now[0] += indexed.MAX_AGE
    assert catalog.leading(DB, graph) == {("City", "name")}


def test_a_catalog_never_read_gives_none_and_is_read_again_after_a_minute(
    graph: str, now: list[float], caplog: pytest.LogCaptureFixture
):
    catalog = Catalog([], failing=RuntimeError("ORA-00942: table or view does not exist"))
    assert catalog.leading(DB, graph) == set()
    assert catalog.leading(DB, graph) == set()
    assert catalog.reads == 1
    catalog.failing = None
    catalog.rows = [row("Person", "name")]
    now[0] += indexed.MAX_AGE
    assert catalog.leading(DB, graph) == {("Person", "name")}
    assert len([it for it in caplog.records if it.levelno == logging.WARNING]) == 1


def test_a_connection_that_names_no_database_reads_and_keeps_nothing(graph: str, now: list[float]):
    catalog = Catalog([row("Person", "name")])
    assert catalog.leading("", graph) == set()
    assert catalog.reads == 0
    assert catalog.leading(DB, graph) == {("Person", "name")}
    assert catalog.reads == 1


# ----- The connection -----


class Cursor:
    def __init__(self, session: "Session") -> None:
        self.session = session
        self.description: Any = None

    def prepare(self, statement: str, tag: str | None = None, cache_statement: bool = True) -> None:
        pass

    async def execute(self, statement: str, parameters: Any = None, **_: Any) -> None:
        self.session.run.append(statement)
        if statement == LABELLED:
            if self.session.failing is not None:
                raise self.session.failing
            self.description = [("R",)]

    async def fetchall(self) -> list[Any]:
        return [(it,) for it in self.session.rows] if self.description else []

    def getimplicitresults(self) -> list[Any]:
        return []

    def close(self) -> None:
        pass


class SyncCursor(Cursor):
    def execute(self, statement: str, parameters: Any = None, **_: Any) -> None:  # type: ignore[override]
        self.session.run.append(statement)
        if statement == LABELLED:
            if self.session.failing is not None:
                raise self.session.failing
            self.description = [("R",)]

    def fetchall(self) -> list[Any]:  # type: ignore[override]
        return [(it,) for it in self.session.rows] if self.description else []


class Session:
    """A session whose catalog lists `rows`, or fails with `failing`."""

    def __init__(self, rows: list[Any], failing: BaseException | None = None, sync: bool = False) -> None:
        self.rows = rows
        self.failing = failing
        self.sync = sync
        self.run: list[str] = []
        self.autocommit = True

    def cursor(self) -> Cursor:
        return SyncCursor(self) if self.sync else Cursor(self)


@pytest.fixture
def compiled_with(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """The indexed properties each Cypher statement is compiled with, by either connection, given the query."""
    seen: list[Any] = []
    monkeypatch.setattr(indexed, "query", lambda graph: LABELLED)
    for module in (oracle_connection, sync_oracle_connection):
        original = module.cypher_compiled

        def recording(*args: Any, _original: Any = original, **kwargs: Any) -> Any:
            seen.append(frozenset(kwargs.get("indexed_properties", ())))
            return _original(*args, **kwargs)

        monkeypatch.setattr(module, "cypher_compiled", recording)
    return seen


STATEMENT = "MATCH (a:Person {id: $s}) RETURN a.k"


async def test_a_cypher_statement_is_compiled_with_the_graphs_labelled_indexes_read_once_on_its_connection(
    graph: str, now: list[float], compiled_with: list[Any]
):
    session = Session([row("Person", "id")])
    connection = OracleConnection(session, None, "s", graph, DB)
    await connection.query(QuerySpecification[Any](cypher_statement(STATEMENT)).bind({"s": "p1"}))
    await connection.query(QuerySpecification[Any](cypher_statement(STATEMENT)).bind({"s": "p2"}))
    assert compiled_with[-1] == {("Person", "id")}
    assert session.run.count(LABELLED) == 1


async def test_plain_sql_reads_no_catalog(graph: str, now: list[float], compiled_with: list[Any]):
    session = Session([row("Person", "id")])
    await OracleConnection(session, None, "s", graph, DB).query(QuerySpecification[Any]("SELECT 1 FROM DUAL"))
    assert LABELLED not in session.run


async def test_a_catalog_that_cant_be_read_compiles_the_statement_with_none_and_runs_it(
    graph: str, now: list[float], compiled_with: list[Any]
):
    session = Session([], failing=RuntimeError("ORA-00942: table or view does not exist"))
    await OracleConnection(session, None, "s", graph, DB).query(
        QuerySpecification[Any](cypher_statement(STATEMENT)).bind({"s": "p1"})
    )
    assert compiled_with[-1] == set()
    assert session.run[0] == LABELLED and len(session.run) > 1, session.run


def test_a_sync_connection_compiles_with_them_too(graph: str, now: list[float], compiled_with: list[Any]):
    session = Session([row("Person", "id")], sync=True)
    SyncOracleConnection(session, None, "s", graph, DB).query(
        QuerySpecification[Any](cypher_statement(STATEMENT)).bind({"s": "p1"})
    )
    assert compiled_with[-1] == {("Person", "id")}


def test_a_statement_compiled_knowing_an_index_compares_its_property_as_the_index_is_made():
    without = compile_cypher(STATEMENT, {"s": "p1"}, "ORACLE", "g").sql
    knowing = compile_cypher(STATEMENT, {"s": "p1"}, "ORACLE", "g", indexed_properties=[("Person", "id")]).sql
    # The labelled index's key, the property's class and key for the label's nodes alone, as the index is made over it.
    key = """CASE WHEN JSON_EXISTS(n1.labels, '$[*]?(@ == "Person")') AND JSON_EXISTS(n1.props, '$.id') THEN"""
    assert key in knowing, knowing
    assert key not in without, without
