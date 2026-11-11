"""
The numbers that stand for NaN and the infinities in the rows of a statement given one, as drivine4j's compiler writes
them on Postgres, read back as the doubles they are where the statement says so, and as today where it doesn't
(liberation-data/drivine#673).
"""

import copy
import math
import sys
from collections.abc import AsyncIterator
from typing import Any

import pytest

from drivine import CursorSpecification, DatabaseRegistry, QuerySpecification
from drivine.connection.connection import Connection
from drivine.connection.postgres.postgres_connection import PostgresConnection
from drivine.cypher.sql.compiled_cypher import CompiledCypher
from drivine.query.statement import Statement
from tests.integration.postgres.conftest import ON_POSTGRES

pytestmark = ON_POSTGRES

NUMBERS = (
    "jsonb_build_array(to_jsonb(2e309 + 0.5), to_jsonb(1e309 + 0.5), to_jsonb(-1e309 - 0.5), to_jsonb(1e308), "
    "to_jsonb(2.5), to_jsonb('2' || repeat('0', 309) || '.5'))"
)
"""The numbers drivine4j's compiler writes for NaN and the infinities, two that stand for neither, and a string."""

STATEMENT = f"SELECT {NUMBERS} AS result"

AS_TODAY = [math.inf, math.inf, -math.inf, 10**308, 2.5, "2" + "0" * 309 + ".5"]
"""As json.loads reads them, as plain SQL's rows are: Postgres writes `numeric` 1e308 as its digits, a whole number."""

COMPILED = [*AS_TODAY[:3], 1e308, *AS_TODAY[4:]]
"""
As a compiled statement's rows are read: the whole number past a long's range is the float nearest it, as drivine4j
reads it (the spec's host.yaml, "Postgres rows of a statement given NaN or an infinity").
"""


def compiled_as(monkeypatch: pytest.MonkeyPatch, non_finite: bool, prelude: str | None = None) -> None:
    """
    Has the Postgres connection run each Cypher statement's text as SQL, compiled with `non_finite` as given, as
    drivine4j's compiler would hand it over.
    """

    def compiled(
        spec: QuerySpecification[Any], *_: Any, **__: Any
    ) -> tuple[QuerySpecification[Any], CompiledCypher | None]:
        if spec.statement is None or spec.statement.language != "CYPHER":
            return spec, None
        sql = copy.copy(spec)
        sql.statement = Statement(spec.statement.text, "SQL")
        sql.parameters = {}
        return sql, CompiledCypher(spec.statement.text, {}, setup=(prelude,) if prelude else (), non_finite=non_finite)

    monkeypatch.setattr(sys.modules[PostgresConnection.__module__], "cypher_compiled", compiled)


@pytest.fixture
async def connection(registry: DatabaseRegistry) -> AsyncIterator[Connection]:
    provider = registry.connection_provider("postgres")
    assert provider is not None
    connection = await provider.connect()
    assert isinstance(connection, PostgresConnection)
    try:
        yield connection
    finally:
        await connection.release()


def same(read: list[Any], expected: list[Any]) -> bool:
    """Whether the values are the same, NaN the same as NaN."""
    return len(read) == len(expected) and all(
        (isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b)) or a == b
        for a, b in zip(read, expected, strict=True)
    )


@pytest.mark.parametrize("prelude", [None, "SELECT 1"])
async def test_reads_the_stand_ins_as_nan_and_the_infinities_where_the_statement_says_so(
    connection: Connection, monkeypatch: pytest.MonkeyPatch, prelude: str | None
):
    compiled_as(monkeypatch, True, prelude)
    [row] = await connection.query(QuerySpecification[Any](Statement(STATEMENT, "CYPHER")))
    assert same(row, [math.nan, math.inf, -math.inf, 1e308, 2.5, "2" + "0" * 309 + ".5"])
    # The cursor's alone: the next statement, not given NaN, reads its stand-ins as numbers.
    compiled_as(monkeypatch, False, prelude)
    [row] = await connection.query(QuerySpecification[Any](Statement(STATEMENT, "CYPHER")))
    assert same(row, COMPILED)


@pytest.mark.parametrize("prelude", [None, "SELECT 1"])
async def test_reads_the_stand_ins_as_today_where_the_statement_doesnt_say_so(
    connection: Connection, monkeypatch: pytest.MonkeyPatch, prelude: str | None
):
    compiled_as(monkeypatch, False, prelude)
    [row] = await connection.query(QuerySpecification[Any](Statement(STATEMENT, "CYPHER")))
    assert same(row, COMPILED)


@pytest.mark.parametrize(("non_finite", "expected"), [(True, [math.nan, math.inf, -math.inf]), (False, AS_TODAY[:3])])
@pytest.mark.parametrize("prelude", [None, "SELECT 1"])
async def test_reads_a_cursors_rows_as_the_statement_says(
    connection: Connection,
    monkeypatch: pytest.MonkeyPatch,
    non_finite: bool,
    expected: list[float],
    prelude: str | None,
):
    compiled_as(monkeypatch, non_finite, prelude)
    statement = (
        "SELECT to_jsonb(it) AS result FROM unnest(ARRAY[2e309 + 0.5, 1e309 + 0.5, -1e309 - 0.5]) WITH ORDINALITY "
        "AS t(it, i) ORDER BY i"
    )
    cursor = await connection.open_cursor(CursorSpecification[Any](Statement(statement, "CYPHER")).batch_size(2))
    try:
        read = [it async for it in cursor]
    finally:
        await cursor.close()
    assert same(read, expected)


async def test_reads_plain_sql_as_today(connection: Connection, monkeypatch: pytest.MonkeyPatch):
    compiled_as(monkeypatch, True)
    [row] = await connection.query(QuerySpecification[Any](STATEMENT))
    assert same(row["result"], AS_TODAY)
