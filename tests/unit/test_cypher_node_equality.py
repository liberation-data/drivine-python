"""
#396, liberation-data/drivine4j#576: a node equal to a node is their keys compared, through the store's own comparison
of two keys (`same_key`). On Oracle that is `+ 0` on one side, both ways round, so either is read by its key: compared
column to column, Oracle 23 dropped the later node's join and tested its label in a form that misses a node with more
than one label, so `MATCH (p)-[:KNOWS]->(q) MATCH (c:Person) WHERE c = q` gave no rows. drivine4j's NodeEqualityTest is
the reference; tests/integration/oracle/test_oracle_node_equality.py checks Oracle's plan.
"""

import re

import pytest

from drivine.cypher.sql.oracle_cypher_compiler import OracleCypherCompiler
from drivine.cypher.sql.postgres_cypher_compiler import PostgresCypherCompiler
from drivine.cypher.sql.sql_cypher_compiler import SqlCypherCompiler
from drivine.cypher.sql.sqlite_cypher_compiler import SqliteCypherCompiler

LATER = "MATCH (p {name: 'Keanu'})-[:KNOWS]->(q) MATCH (c:Person) WHERE c = q RETURN c.name"
EARLIER = "MATCH (p {name: 'Keanu'})-[:KNOWS]->(q) MATCH (c:Person) WHERE q = c RETURN c.name"
Q = "MATCH (p {name: 'Keanu'})-[:KNOWS]->(q)"


def sql_of(compiler: type[SqlCypherCompiler], cypher: str) -> str:
    return compiler("g").compile(cypher, {}).sql


def test_on_oracle_a_node_equal_to_a_node_compares_the_keys_with_plus_0_on_one_side_both_ways_round() -> None:
    sql = sql_of(OracleCypherCompiler, LATER)
    assert "(n4.nid = n2.nid + 0 AND n2.nid = n4.nid + 0)" in sql, sql
    other = sql_of(OracleCypherCompiler, EARLIER)
    assert "(n2.nid = n4.nid + 0 AND n4.nid = n2.nid + 0)" in other, other


def test_on_oracle_an_id_equal_to_an_id_compares_the_keys_both_ways_round_too() -> None:
    sql = sql_of(OracleCypherCompiler, f"{Q} MATCH (c:Person) WHERE id(c) = id(q) RETURN c.name")
    assert "(n4.nid = n2.nid + 0 AND n2.nid = n4.nid + 0)" in sql, sql


@pytest.mark.parametrize(
    "statement",
    [
        f"{Q} MATCH (c:Person) WHERE c <> q RETURN c.name",
        f"{Q} MATCH (c:Person) WHERE c IN [q] RETURN c.name",
        f"{Q} MATCH (c:Person) WHERE [c] = [q] RETURN c.name",
        f"{Q} MATCH (c:Person) WHERE CASE c WHEN q THEN true ELSE false END RETURN c.name",
        f"{Q} OPTIONAL MATCH (c:Person) WHERE c = q RETURN c.name",
        f"{Q} WHERE EXISTS {{ MATCH (c:Person) WHERE c = q }} RETURN q.name",
        f"{Q} RETURN COUNT {{ MATCH (c:Person) WHERE c = q }}",
    ],
)
def test_on_oracle_a_node_compared_to_a_node_by_not_equal_in_a_list_case_or_in_a_subquery_compares_the_keys_with_plus_0(
    statement: str,
) -> None:
    sql = sql_of(OracleCypherCompiler, statement)
    assert re.search(r"\(n\d+\.nid = n\d+\.nid \+ 0 AND n\d+\.nid = n\d+\.nid \+ 0\)", sql), sql
    assert not re.search(r"n\d+\.nid = n\d+\.nid(?! \+ 0)", sql), sql


@pytest.mark.parametrize("compiler", [PostgresCypherCompiler, SqliteCypherCompiler], ids=lambda it: it.__name__)
def test_on_postgres_and_sqlite_a_node_equal_to_a_node_compares_the_keys_as_they_are(
    compiler: type[SqlCypherCompiler],
) -> None:
    sql = sql_of(compiler, LATER)
    assert "n4.nid = n2.nid" in sql and "+ 0" not in sql, sql


def test_a_relationship_equal_to_a_relationship_still_compares_the_keys_as_they_are_on_oracle_too() -> None:
    sql = sql_of(OracleCypherCompiler, "MATCH (a)-[r]->(b) MATCH (a)-[s]->(b) WHERE s = r RETURN count(*)")
    assert re.search(r"e\d+\.eid = e\d+\.eid\b(?! \+ 0)", sql), sql
