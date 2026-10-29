"""
On Oracle, a query that reads the graph's tables twice or more turns the JSON table transformation off, which lost a
node's label test beside another node's table (#258, liberation-data/drivine4j#415); the hint is the statement's, so its
outermost query holds it. A query of one table is written as it was. drivine4j's OracleJsonTableTransformTest, ported.
"""

import re

from drivine.cypher.sql.oracle_cypher_compiler import OracleCypherCompiler
from drivine.cypher.sql.postgres_cypher_compiler import PostgresCypherCompiler

HINT = "NO_JSON_TABLE_TRANSFORM"


def compiled(cypher: str, graph: str = "g") -> str:
    return OracleCypherCompiler(graph).compile(cypher).sql


def test_a_query_of_two_tables_opens_with_the_hint_once():
    sql = compiled("MATCH (a:N1), (b:N2) RETURN a.n + b.n, a.n - b.n")
    assert sql.startswith(f"SELECT /*+ {HINT} */ "), sql
    assert sql.count(HINT) == 1, sql
    assert compiled("MATCH (a:N1)-->(b:N2) RETURN b.n + 1").startswith(f"SELECT /*+ {HINT} */ ")


def test_a_query_of_one_table_has_no_hint():
    for cypher in [
        "MATCH (a:N1) RETURN a.n + 1, a.n - 1",
        "UNWIND [1, 2] AS x MATCH (a:N1) RETURN a.n - a.m, x",
        "RETURN 1",
    ]:
        sql = compiled(cypher)
        assert HINT not in sql, f"{cypher}: {sql}"


def test_a_unions_outermost_query_holds_the_hint_and_a_querys_declared_function_stays_before_it():
    union = compiled("MATCH (a:N1), (b:N2) RETURN a.n + b.n AS s UNION ALL MATCH (a:N2) RETURN a.n AS s")
    assert union.startswith(f"SELECT /*+ {HINT} */ u.result"), union
    declared = compiled("MATCH (a:N1), (b:N2) RETURN reduce(t = 0, x IN [a.n, b.n] | t + x)")
    assert declared.startswith("WITH\nFUNCTION ") and f"\nSELECT /*+ {HINT} */ " in declared, declared


def test_a_statement_that_writes_has_the_hint_in_each_of_its_queries_that_read_two_tables():
    sql = compiled("MATCH (a:N1), (b:N2) SET a.x = a.n + b.n RETURN a.x")
    assert sql.startswith("DECLARE"), sql
    assert re.search(rf"SELECT /\*\+ {HINT} \*/ [^;]*INTO", sql), sql
    # A MERGE's match of two tables loops over the query, which holds the hint.
    merged = compiled("MATCH (a:N1) MERGE (a)-[:R]->(b:N2) RETURN b.n")
    assert re.search(rf"FOR \w+ IN \(\s*SELECT /\*\+ {HINT} \*/", merged), merged
    # The rows it returns, read from two tables, are a query of their own, which holds the hint.
    created = compiled("CREATE (a:N1 {n: 1})-[:R]->(b:N2 {n: 2}) RETURN a.n, b.n")
    assert re.search(rf"OPEN \w+ FOR[^;]*SELECT /\*\+ {HINT} \*/", created), created


def test_the_graphs_own_tables_are_counted_and_the_hints_a_query_opens_with_are_joined():
    assert HINT in compiled("MATCH (a:N1), (b:N2) RETURN a.n + b.n", "g.x")
    # A graph whose name a regular expression would read otherwise, `g.x`, counts no other graph's table.
    assert HINT not in OracleCypherCompiler("g.x").planned("SELECT 1 FROM gyx_node n1 CROSS JOIN gyx_node n2")
    assert (
        OracleCypherCompiler("g").planned("SELECT /*+ NO_MERGE */ * FROM g_node n1, g_edge e2")
        == f"SELECT /*+ NO_MERGE {HINT} */ * FROM g_node n1, g_edge e2"
    )


def test_postgres_writes_the_query_as_before_with_no_hint():
    sql = PostgresCypherCompiler("g").compile("MATCH (a:N1), (b:N2) RETURN a.n + b.n").sql
    assert HINT not in sql
    assert sql.startswith("SELECT ")
