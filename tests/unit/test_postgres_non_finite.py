"""
How the rows of a statement given NaN or an infinity are read on Postgres, as drivine4j's `PostgresCypherCompiler.read`
reads them, and which statements are read so: those that say they were given one, alone (liberation-data/drivine#673).
"""

import json
import math
from typing import Any

import psycopg
import pytest
from psycopg.adapt import AdaptersMap
from psycopg.pq import Format

from drivine.connection.postgres import postgres_connection
from drivine.connection.postgres.postgres_non_finite import non_finite_of, read_non_finite, read_non_finite_json
from drivine.cursor.cursor_specification import CursorSpecification
from drivine.cypher.cypher_specification import cypher_compiled
from drivine.cypher.sql.compiled_cypher import CompiledCypher
from drivine.query.query_specification import QuerySpecification
from drivine.query.statement import Statement
from drivine.sync.connection.postgres import postgres_connection as sync_postgres_connection

NAN = "2" + "0" * 309 + ".5"
"""The number drivine4j's compiler writes on Postgres for NaN, as `jsonb` writes it."""

INFINITY = "1" + "0" * 309 + ".5"
NEGATIVE_INFINITY = f"-{INFINITY}"

JSON_OID = 114
JSONB_OID = 3802


def is_nan(value: Any) -> bool:
    return isinstance(value, float) and math.isnan(value)


@pytest.mark.parametrize(
    ("text", "number"),
    [
        (NAN, math.nan),
        (f"{NAN}0", math.nan),
        (f"{NAN}e0", math.nan),
        (f"{NAN}E+0", math.nan),
        (f"0.2{'0' * 309}5e310", math.nan),
        (f"2{'0' * 309}5e-1", math.nan),
        (INFINITY, math.inf),
        (NEGATIVE_INFINITY, -math.inf),
        ("1e309", math.inf),
        ("1" + "0" * 309, math.inf),
        ("2e309", math.inf),
        (f"2{'0' * 309}.4", math.inf),
        (f"2{'0' * 309}.6", math.inf),
        ("-2e309", -math.inf),
        (f"-{NAN}", -math.inf),
        ("-1" + "0" * 309, -math.inf),
        ("1e400", math.inf),
    ],
)
def test_reads_a_stand_in_as_the_double_it_stands_for(text: str, number: float):
    found = non_finite_of(text)
    assert is_nan(found) if math.isnan(number) else found == number
    [read] = read_non_finite_json(f"[{text}]")
    assert is_nan(read) if math.isnan(number) else read == number


@pytest.mark.parametrize(
    "text",
    [
        "1e308",
        "-1e308",
        "1.7976931348623157e308",
        "1.8e308",
        "9" * 309,
        "-" + "9" * 309,
        f"{'9' * 309}.9",
        "0",
        "-0",
        "-0.0",
        "2.5",
        "12345678901234567890123",
        "0.0000000000000000000000000000000000000000000000000001",
    ],
)
def test_reads_any_other_number_as_json_loads_does(text: str):
    read = read_non_finite_json(text)
    today = json.loads(text)
    assert read == today
    assert type(read) is type(today)
    if isinstance(today, float):
        assert math.copysign(1, read) == math.copysign(1, today)


@pytest.mark.parametrize(
    ("text", "number"), [("1e99999999999999999999", math.inf), ("-1e99999999999999999999", -math.inf)]
)
def test_reads_a_number_past_decimals_exponent_as_json_loads_does(text: str, number: float):
    assert read_non_finite_json(f"[{text}]") == json.loads(f"[{text}]") == [number]


def test_reads_the_stand_ins_nested_in_lists_and_maps_and_leaves_the_rest():
    text = (
        f'{{"a": [1, {NAN}, {{"b": {INFINITY}}}], "c": {NEGATIVE_INFINITY}, "d": "{NAN}", "e": 2e309, "f": 1e308, '
        f'"g": true, "h": null}}'
    )
    read = read_non_finite_json(text.encode())
    assert is_nan(read["a"][1])
    read["a"][1] = "NaN"
    assert read == {
        "a": [1, "NaN", {"b": math.inf}],
        "c": -math.inf,
        "d": NAN,
        "e": math.inf,
        "f": 1e308,
        "g": True,
        "h": None,
    }


def test_reads_the_stand_in_for_nan_as_json_loads_does_an_infinity_where_not_asked_to():
    assert json.loads(NAN) == math.inf
    assert json.loads(f'{{"a": [{NAN}]}}') == {"a": [math.inf]}


class Cursor:
    """A cursor of psycopg's that reads its JSON as `adapters` say, as psycopg's own reads it."""

    def __init__(self) -> None:
        self.adapters = AdaptersMap(psycopg.adapters)


def loaded(adapters: AdaptersMap, oid: int, format: Format, text: str) -> Any:
    loader = adapters.get_loader(oid, format)
    assert loader is not None
    data = text.encode()
    return loader(oid).load(b"\x01" + data if (oid, format) == (JSONB_OID, Format.BINARY) else data)


@pytest.mark.parametrize("oid", [JSON_OID, JSONB_OID])
@pytest.mark.parametrize("format", [Format.TEXT, Format.BINARY])
def test_has_a_cursor_read_json_and_jsonb_for_nan_and_nothing_else(oid: int, format: Format):
    cursor = Cursor()
    read_non_finite(cursor)
    assert is_nan(loaded(cursor.adapters, oid, format, f"[{NAN}]")[0])
    # Nothing is set for the process, nor for another cursor.
    assert loaded(psycopg.adapters, oid, format, f"[{NAN}]") == [math.inf]
    assert loaded(Cursor().adapters, oid, format, f"[{NAN}]") == [math.inf]


CYPHER = [
    "MATCH (n) RETURN n.x",
    "MATCH (n:A {x: 1.5}) RETURN n.x",
    "CREATE (n:A {x: $x}) RETURN n.x",
    "MATCH (n:A) SET n.x = 1 RETURN n",
    "RETURN 1 AS x UNION RETURN 2 AS x",
    "UNWIND [1, 2] AS x RETURN x ORDER BY x",
]


@pytest.mark.parametrize("cypher", CYPHER)
@pytest.mark.parametrize("store", ["POSTGRES", "ORACLE", "SQLITE"])
def test_drivines_own_compiler_says_no_statement_was_given_nan(cypher: str, store: Any):
    _, compiled = cypher_compiled(QuerySpecification[Any](Statement(cypher, "CYPHER")).bind({"x": 2}), store, "g")
    assert compiled is not None
    assert compiled.non_finite is False


def test_a_compiled_statement_says_it_was_given_no_nan_unless_told():
    assert CompiledCypher("SELECT 1", {}).non_finite is False
    assert CompiledCypher("SELECT 1", {}, non_finite=True).non_finite is True


ROW = {"result": 1}


class AsyncCursor:
    """A psycopg cursor that gives the row ROW, and records whether its JSON was read for NaN."""

    def __init__(self, record: list[str]) -> None:
        self.record = record
        self.description = [("result",)]
        self._adapters: AdaptersMap | None = None

    @property
    def adapters(self) -> AdaptersMap:
        self.record.append("adapters")
        if self._adapters is None:
            self._adapters = AdaptersMap(psycopg.adapters)
        return self._adapters

    async def __aenter__(self) -> "AsyncCursor":
        return self

    async def __aexit__(self, *_: Any) -> None:
        pass

    async def execute(self, statement: str, parameters: Any = None) -> None:
        self.record.append(statement)

    async def fetchall(self) -> list[dict[str, Any]]:
        return [ROW]

    async def fetchmany(self, _: int) -> list[dict[str, Any]]:
        return [ROW]

    async def close(self) -> None:
        pass


class SyncCursor(AsyncCursor):
    def __enter__(self) -> "SyncCursor":
        return self

    def __exit__(self, *_: Any) -> None:
        pass

    def execute(self, statement: str, parameters: Any = None) -> None:  # type: ignore[override]
        self.record.append(statement)

    def fetchall(self) -> list[dict[str, Any]]:  # type: ignore[override]
        return [ROW]

    def fetchmany(self, _: int) -> list[dict[str, Any]]:  # type: ignore[override]
        return [ROW]

    def close(self) -> None:  # type: ignore[override]
        pass


class Client:
    """A psycopg connection whose cursors are `cursor_type`'s."""

    def __init__(self, cursor_type: type[AsyncCursor]) -> None:
        self.record: list[str] = []
        self.cursors: list[AsyncCursor] = []
        self.cursor_type = cursor_type

    def cursor(self, **_: Any) -> AsyncCursor:
        cursor = self.cursor_type(self.record)
        self.cursors.append(cursor)
        return cursor

    async def execute(self, statement: str) -> None:
        self.record.append(statement)


class SyncClient(Client):
    def execute(self, statement: str) -> None:  # type: ignore[override]
        self.record.append(statement)


def compiled_as(monkeypatch: pytest.MonkeyPatch, module: Any, non_finite: bool, prelude: str | None) -> None:
    """Has the connection of `module` compile each Cypher statement to `SELECT 2`, with `non_finite` as given."""

    def compiled(spec: QuerySpecification[Any], *_: Any) -> tuple[QuerySpecification[Any], CompiledCypher | None]:
        if spec.statement is None or spec.statement.language != "CYPHER":
            return spec, None
        sql = QuerySpecification[Any](Statement("SELECT 2", "SQL"))
        return sql, CompiledCypher("SELECT 2", {}, prelude=prelude, non_finite=non_finite)

    monkeypatch.setattr(module, "cypher_compiled", compiled)


async def run(sync: bool, how: str, spec: Any) -> tuple[list[Any], Client]:
    """The rows `spec` gives through a Postgres connection, async or `sync`, by `query` or a cursor, and its client."""
    module = sync_postgres_connection if sync else postgres_connection
    client = (SyncClient if sync else Client)(SyncCursor if sync else AsyncCursor)
    connection: Any = module.PostgresConnection(client, lambda _: None, "pg.1")
    if how == "query":
        rows = connection.query(QuerySpecification[Any](spec))
        return (rows if sync else await rows), client
    cursor = connection.open_cursor(CursorSpecification[Any](spec).batch_size(5))
    cursor = cursor if sync else await cursor
    return (cursor.read(5) if sync else await cursor.read(5))[0], client


@pytest.mark.parametrize("sync", [False, True])
@pytest.mark.parametrize("how", ["query", "cursor"])
@pytest.mark.parametrize("prelude", [None, "PRELUDE"])
async def test_a_connection_reads_for_nan_only_a_statement_that_says_it_was_given_one(
    monkeypatch: pytest.MonkeyPatch, sync: bool, how: str, prelude: str | None
):
    module = sync_postgres_connection if sync else postgres_connection
    compiled_as(monkeypatch, module, False, prelude)
    rows, client = await run(sync, how, Statement("RETURN 1", "CYPHER"))
    assert rows == [1]
    # Not given NaN, its cursor is left as psycopg made it: never even asked for its adapters.
    assert "adapters" not in client.record
    assert client.record == [it for it in [prelude, "SELECT 2"] if it is not None]

    compiled_as(monkeypatch, module, True, prelude)
    rows, client = await run(sync, how, Statement("RETURN 1", "CYPHER"))
    assert rows == [1]
    [cursor] = client.cursors
    assert cursor._adapters is not None
    assert is_nan(loaded(cursor._adapters, JSONB_OID, Format.TEXT, f"[{NAN}]")[0])
    # Its JSON is read for NaN before the statement runs.
    assert client.record.index("adapters") < client.record.index("SELECT 2")


@pytest.mark.parametrize("sync", [False, True])
@pytest.mark.parametrize("how", ["query", "cursor"])
async def test_a_connection_reads_plain_sql_and_drivines_own_compiled_statements_as_psycopg_does(sync: bool, how: str):
    for spec in [Statement("SELECT 1", "SQL"), Statement("RETURN 1 AS x", "CYPHER")]:
        _, client = await run(sync, how, spec)
        assert "adapters" not in client.record
