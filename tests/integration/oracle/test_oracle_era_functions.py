"""
The functions an Oracle graph is made with that renumber a year BC, `<graph>_era` and `<graph>_era_date`, which a
compiled Cypher statement reads and writes a date or a moment by (liberation-data/drivine#572), and what happens where a
store has none, or has them and not `<graph>_nulls_equal`: drivine4j's OracleFreshGraphTest,
OracleTablesOnFirstConnectionTest and CypherWriteOracleTest of liberation-data/drivine4j#584.
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest

from drivine.backend.oracle.oracle_graph import OracleGraph
from drivine.backend.oracle.oracle_stored_functions import OracleStoredFunctions
from tests.integration.oracle.oracle_seed import ON_ORACLE
from tests.integration.oracle.test_oracle_nulls_equal import (
    answer,
    cypher,
    dropping,
    provider_for,
    run,
    session_of,
)

pytestmark = ON_ORACLE

MIXED = (
    "CREATE (n:CyTemporal {id: 'm', day: date('-0044-03-15') + duration('P1D')}) WITH n, [null] AS a, [1] AS b "
    "RETURN [coalesce(a = b, 'null'), toString(n.day)]"
)
"""A statement that writes, calling both the era functions and `<graph>_nulls_equal`."""


@pytest.fixture
async def graph(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
    """
    A graph of the test's own, dropped before and after it, and nothing remembered of any session: a session found to
    call a function is remembered for the process, and these tests take the functions away.
    """
    monkeypatch.setattr(OracleStoredFunctions, "_callable", set())
    monkeypatch.setattr(OracleStoredFunctions, "_absent", {})
    monkeypatch.setattr(OracleStoredFunctions, "_warned", set())
    name = f"era_{request.node.name.removeprefix('test_')[:24]}"
    await run(dropping(name))
    yield name
    await run(dropping(name))


async def made(graph: str) -> Any:
    """The graph's functions that are there and valid, by name, between commas."""
    names = ", ".join(f"'{it.upper()}'" for it in (OracleGraph(graph).nulls_equal, *era_functions(graph)))
    return await answer(
        "SELECT LISTAGG(object_name, ',') WITHIN GROUP (ORDER BY object_name) FROM user_objects "
        f"WHERE object_type = 'FUNCTION' AND status = 'VALID' AND object_name IN ({names})"
    )


def era_functions(graph: str) -> tuple[str, str]:
    return OracleGraph(graph).era, OracleGraph(graph).era_date


async def test_the_graph_is_made_with_the_functions_a_cypher_statement_renumbers_a_year_bc_by(graph: str):
    # Each reads its text once, where the SQL it stands for read it three or four times.
    await run([OracleGraph(graph).ensuring])
    era, era_date = era_functions(graph)
    texts = ["'0000-03-15'", "'-0044-03-15T12:00:00'", "'0999-12-31'", "'2024-03-15'", "NULL"]
    read = " || ',' || ".join(f"NVL({era}({it}), 'null')" for it in texts)
    assert await answer(f"SELECT {read} FROM DUAL") == "-0001-03-15,-0045-03-15T12:00:00,0999-12-31,2024-03-15,null"
    texts = ["'-0001-03-15'", "'-0045-03-15T12:00:00'", "'0999-12-31'", "'2024-03-15'", "NULL"]
    written = " || ',' || ".join(f"NVL({era_date}({it}), 'null')" for it in texts)
    assert await answer(f"SELECT {written} FROM DUAL") == "0000-03-15,-0044-03-15T12:00:00,0999-12-31,2024-03-15,null"


async def test_a_store_an_earlier_version_made_gains_the_functions_it_lacks_when_the_graph_is_ensured(graph: str):
    # A store made before drivine#572 has its nulls_equal and not the era functions: ensured, it gains them, and keeps
    # the other.
    provider = provider_for(graph)
    connection = await session_of(provider)
    try:
        await run([f"DROP FUNCTION {it}" for it in era_functions(graph)])
        assert not await connection._calls_era()
        assert await connection._calls_nulls_equal()
        await run([OracleGraph(graph).ensuring])
        OracleGraph(graph).ensured()
        assert await connection._calls_era()
        upper = graph.upper()
        assert await made(graph) == f"{upper}_ERA,{upper}_ERA_DATE,{upper}_NULLS_EQUAL"
        # One of the two missing is made on its own.
        await run([f"DROP FUNCTION {OracleGraph(graph).era_date}"])
        await run([OracleGraph(graph).ensuring])
        assert await made(graph) == f"{upper}_ERA,{upper}_ERA_DATE,{upper}_NULLS_EQUAL"
    finally:
        await connection.release()
        await provider.end()


async def test_a_session_that_may_call_only_one_of_the_era_functions_is_taken_to_call_neither(graph: str):
    provider = provider_for(graph)
    connection = await session_of(provider)
    try:
        await run([f"DROP FUNCTION {OracleGraph(graph).era_date}"])
        assert not await connection._calls_era()
    finally:
        await connection.release()
        await provider.end()


async def test_a_store_with_its_tables_and_not_its_functions_gains_them_on_a_providers_first_connection(graph: str):
    # A store an earlier version made has its tables and not the functions: a pool's first connection makes them, and a
    # session found to call none is asked again.
    provider = provider_for(graph)
    try:
        await cypher(provider, "RETURN 1")
        functions = [OracleGraph(graph).nulls_equal, *era_functions(graph)]
        await run([f"DROP FUNCTION {it}" for it in functions])
        connection = await session_of(provider)
        try:
            assert not await connection._calls_nulls_equal()
            assert not await connection._calls_era()
        finally:
            await connection.release()
        await provider.end()
        connection = await session_of(provider)
        try:
            assert await connection._calls_nulls_equal(), "asked again once the provider has ensured the graph"
            assert await connection._calls_era(), "asked again once the provider has ensured the graph"
        finally:
            await connection.release()
        upper = graph.upper()
        assert await made(graph) == f"{upper}_ERA,{upper}_ERA_DATE,{upper}_NULLS_EQUAL"
    finally:
        await provider.end()


async def test_a_statement_that_would_call_the_era_functions_and_nulls_equal_is_compiled_to_call_only_the_era_ones(
    graph: str,
):
    # The store keeps its era functions and not nulls_equal: compiled again, the statement calls them and not
    # nulls_equal, so a write that compares [null] with [1] gives false. The other way round is
    # test_oracle_temporal_without_era's.
    provider = provider_for(graph)
    try:
        await cypher(provider, "RETURN 1")
        await run([f"DROP FUNCTION {OracleGraph(graph).nulls_equal}"])
        assert await cypher(provider, MIXED) == [[False, "-0044-03-16"]]
    finally:
        await provider.end()
