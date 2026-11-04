"""
A relationship of either direction is compiled to read the relationship table both ways round and join on equalities on
Postgres and Oracle (#390, liberation-data/drivine4j#570), a relationship from a node to itself kept once by a condition
of the join, not a `WHERE` of either half. SQLite keeps the OR, which it probes by index. It joined with an OR across
the two directions, which Postgres can't hash-join on, so it compared every pair of nodes, and which Oracle's recursive
walk didn't expand, so it read every relationship at each step. The rows it gives are pinned by the Cypher spec's
undirected cases and tests/integration/test_cypher_compiled.py; this pins the shape of the join. drivine4j's
UndirectedJoinTest is the reference.
"""

import re

import pytest

from drivine.cypher.sql.oracle_cypher_compiler import OracleCypherCompiler
from drivine.cypher.sql.postgres_cypher_compiler import PostgresCypherCompiler
from drivine.cypher.sql.sql_cypher_compiler import SqlCypherCompiler
from drivine.cypher.sql.sqlite_cypher_compiler import SqliteCypherCompiler

GRAPH = "undirected"

BOTH_WAYS: list[type[SqlCypherCompiler]] = [PostgresCypherCompiler, OracleCypherCompiler]
"""The stores that read a relationship of either direction both ways round; SQLite keeps the OR."""

UNDIRECTED = [
    "MATCH (n)-[r]-(m) RETURN n, r, m",
    "MATCH (n {k: 1})-[r:KNOWS]-(m) RETURN r.since, m",
    "MATCH (a {k: 1}), (b {k: 2}) MATCH (a)-[r]-(b) RETURN r",
    "MATCH (n {k: 1})-[*1..3]-(m) RETURN m",
    "MATCH p = (n)-[:KNOWS*1..2]-(m) RETURN nodes(p), relationships(p)",
    "MATCH (m)-[*1..2]-(n {k: 1}) RETURN m",
    "MATCH (n {k: 1}) ((x)-[:KNOWS]-(y)-[]-(z)){1,2} (m) RETURN m",
    "MATCH (a {k: 1}), (b {k: 2}) MATCH p = shortestPath((a)-[*]-(b)) RETURN nodes(p)",
]

END_COMPARED = re.compile(r"\be\d+\.(src|dst) = ")
"""
An end of a relationship row compared as stored: how a step either way round was joined, with an OR across the two
directions, and how its walk found the node it reached, `CASE WHEN e.src = at THEN e.dst ELSE e.src END`. A relationship
row's alias is `e` and a number; Oracle's walk has a `src` column of its own, the node it set out from.
"""

SELF_LOOP_ONCE = re.compile(r"\(e\d+\.back = 0 OR e\d+\.src <> e\d+\.dst\)")


def compiled(compiler: type[SqlCypherCompiler], cypher: str) -> str:
    return compiler(GRAPH).compile(cypher, {}).sql


@pytest.mark.parametrize("compiler", BOTH_WAYS, ids=lambda it: it.__name__)
@pytest.mark.parametrize("cypher", UNDIRECTED)
def test_a_relationship_of_either_direction_joins_on_the_ends_read_both_ways_round_with_no_or_across_them(
    compiler: type[SqlCypherCompiler], cypher: str
) -> None:
    sql = compiled(compiler, cypher)
    assert f"UNION ALL SELECT eid, src, dst, type, props, dst AS a, src AS b, 1 AS back FROM {GRAPH}_edge)" in sql, sql
    # A relationship from a node to itself is kept once by the join, not by a WHERE of the second half, which would
    # keep Postgres from reading each half by its index (liberation-data/drivine4j#574).
    assert SELF_LOOP_ONCE.search(sql), sql
    assert "WHERE src <> dst" not in sql, sql
    assert END_COMPARED.search(sql) is None, sql


@pytest.mark.parametrize("cypher", UNDIRECTED)
def test_on_sqlite_a_relationship_of_either_direction_still_joins_with_an_or_read_from_the_relationship_table(
    cypher: str,
) -> None:
    sql = compiled(SqliteCypherCompiler, cypher)
    assert "AS back" not in sql, sql
    assert re.search(r"\be\d+\.src = \S+ OR \be\d+\.dst = |\) OR \(e\d+\.dst = ", sql), sql


@pytest.mark.parametrize("compiler", [*BOTH_WAYS, SqliteCypherCompiler], ids=lambda it: it.__name__)
@pytest.mark.parametrize("cypher", ["MATCH (n)-[r]->(m) RETURN r", "MATCH (n)<-[*1..2]-(m) RETURN m"])
def test_a_directed_relationship_still_reads_the_relationship_table_once(
    compiler: type[SqlCypherCompiler], cypher: str
) -> None:
    sql = compiled(compiler, cypher)
    assert "dst AS a" not in sql, sql
    assert END_COMPARED.search(sql) is not None, sql
