"""
A write that loops over its rows keeps each row it leaves in a list the store extends in place, and reads them back as
one JSON list where a step reads them (#419, liberation-data/drivine4j#588): the rows are those Neo4j gives, in their
order, with their nulls and nested values, and a `CALL { … }` that writes and returns gives each row's in turn. The
rows are Neo4j's. drivine4j's `CypherWriteCrossEngineTest`, its tests for #588, and Drivine for TypeScript's
`CypherWriteLoop.spec.ts`. How the time of such a write grows with its rows is timed by
tests/integration/sql/test_compiled_write_loop_time.py.
"""

from typing import Any

import pytest

from drivine import DatabaseType, PersistenceManager, QuerySpecification
from drivine.query.statement import cypher_statement


async def query(pm: PersistenceManager, cypher: str, parameters: dict[str, Any] | None = None) -> list[Any]:
    return await pm.query(QuerySpecification[Any](cypher_statement(cypher)).bind(parameters))


@pytest.fixture
async def graph(cypher_pm: PersistenceManager) -> PersistenceManager:
    """The store's graph, cleared."""
    if cypher_pm.type is DatabaseType.NEO4J:
        await cypher_pm.execute(QuerySpecification("MATCH (n) DETACH DELETE n"))
    else:
        # A node's relationships go with it: the relationship table cascades.
        await cypher_pm.execute(QuerySpecification("DELETE FROM drivine_node"))
    return cypher_pm


async def test_the_rows_a_create_set_or_remove_leaves_are_its_rows_in_their_order_with_their_nulls(
    graph: PersistenceManager,
):
    # Each row a write loop keeps is put back in the order it was read, a null and a map of lists as they were.
    assert await query(graph, "UNWIND [3, null, 1, 2] AS i CREATE (n:CyWrite {k: i}) RETURN [i, n.k]") == [
        [3, 3],
        [None, None],
        [1, 1],
        [2, 2],
    ]
    assert await query(graph, "UNWIND [{a: [1, null]}, null] AS m CREATE (:CyWrite {m: true}) RETURN m") == [
        {"a": [1, None]},
        None,
    ]
    assert await query(
        graph, "MATCH (n:CyWrite) WHERE n.k IS NOT NULL WITH n ORDER BY n.k DESC SET n.x = n.k * 10 RETURN n.x"
    ) == [30, 20, 10]
    assert await query(
        graph, "MATCH (n:CyWrite) WHERE n.k IS NOT NULL WITH n ORDER BY n.k REMOVE n.x RETURN [n.k, n.x]"
    ) == [[1, None], [2, None], [3, None]]
    assert await query(graph, "MATCH (n:CyNone) SET n.x = 1 RETURN count(*)") == [0]
    assert await query(graph, "UNWIND [1, 2] AS k FOREACH (x IN [k] | CREATE (:CyWrite {f: x})) RETURN k") == [1, 2]


async def test_the_rows_a_merge_leaves_are_those_of_each_row_in_turn_a_row_for_each_match(graph: PersistenceManager):
    # A MERGE keeps a row for each node it finds, or the one it makes, the input rows in their order.
    await query(graph, "CREATE (:CyWrite {k: 7, t: 'a'}), (:CyWrite {k: 7, t: 'b'})")
    rows = await query(graph, "UNWIND [8, 7, 8] AS k MERGE (n:CyWrite {k: k}) RETURN [k, n.t]")
    assert [it[0] for it in rows] == [8, 7, 7, 8]
    assert {it[1] for it in rows[1:3]} == {"a", "b"}
    assert await query(graph, "MATCH (n:CyWrite) RETURN count(n)") == [3]


async def test_the_rows_a_call_that_writes_and_returns_gives_are_each_rows_in_turn_with_what_it_returns_in_order(
    graph: PersistenceManager,
):
    # What a call returns for each row is kept after what it returned for the rows before it.
    assert await query(
        graph, "UNWIND [1, 2] AS k CALL (k) { UNWIND [k, k * 10] AS j CREATE (:CyWrite {j: j}) RETURN j } RETURN [k, j]"
    ) == [[1, 1], [1, 10], [2, 2], [2, 20]]
    assert await query(
        graph, "UNWIND [1, 2] AS k CALL (k) { CREATE (m:CyWrite) RETURN m.none AS z } RETURN [k, z]"
    ) == [[1, None], [2, None]]
    assert await query(graph, "MATCH (n:CyNone) CALL (n) { SET n.x = 1 RETURN 1 AS one } RETURN count(*)") == [0]
    # A call within a call keeps its rows in a list of its own depth, and two calls in turn each make theirs anew.
    assert await query(
        graph,
        "UNWIND [1, 2] AS k CALL (k) { UNWIND [k, k * 2] AS i CALL (i) { CREATE (:CyWrite {i: i}) RETURN i * 10 AS j } "
        "CREATE (:CyWrite {j: j}) RETURN j } CALL (j) { CREATE (:CyWrite {j: j}) RETURN j + 1 AS l } RETURN [k, j, l]",
    ) == [[1, 10, 11], [1, 20, 21], [2, 20, 21], [2, 40, 41]]


async def test_the_rows_a_write_keeps_hold_their_values_as_a_read_gives_them_exact_numbers_temporals_and_nested_maps(
    graph: PersistenceManager,
):
    # A kept row goes through the store's own list (jsonb[], JSON_ARRAY_T, a list in SQLite's function) and back. Each
    # value is read from the row's item, so the row holds it, not the statement's text. No text past 32 KB: on Oracle a
    # read and a CREATE that return one, in a list, leave the session to crash on its next statement, as before #419's
    # change (liberation-data/drivine4j#599).
    given = {
        "rows": [
            {
                "i": i,
                "big": 9007199254740993,
                "f": 0.1,
                "m": {"a": {"b": [1, "c", None]}},
                "d": "2020-01-02",
                "dt": "2020-01-02T03:04:05.123Z",
            }
            for i in (1, 2)
        ]
    }
    values = (
        "UNWIND $rows AS r WITH r.i AS i, r.big AS big, r.f AS f, r.i / 3.0 AS third, r.m AS m, date(r.d) AS d, "
        "datetime(r.dt) AS dt "
    )
    items = "[i, big, f, third, m, d.year, d.day, dt.epochMillis]"
    read = await query(graph, f"{values}RETURN {items}", given)
    assert len(read) == 2
    assert [*read[0][:3], read[0][4]] == [1, 9007199254740993, 0.1, {"a": {"b": [1, "c", None]}}]
    assert await query(graph, f"{values}CREATE (:CyWrite {{i: i}}) RETURN {items}", given) == read
    assert (
        await query(
            graph,
            f"{values}CALL (i, big, f, third, m, d, dt) {{ CREATE (:CyWrite {{i: i}}) RETURN {items} AS row }} "
            "RETURN row",
            given,
        )
        == read
    )
