"""
On Postgres, a row of a write's or a list's rows reads its node or relationship back by its id through a probe of the
table's key, `LEFT JOIN LATERAL (… WHERE nid = … OFFSET 0)`, not a plain join, which is planned by the table's
statistics (#446, liberation-data/drivine4j#644): see `PostgresCypherCompiler.key_join`. The plans are
tests/integration/postgres/test_postgres_key_probe_plan.py's. drivine4j's PostgresKeyProbeTest, ported.
"""

import re

import pytest

from drivine.cypher.sql.oracle_cypher_compiler import OracleCypherCompiler
from drivine.cypher.sql.postgres_cypher_compiler import PostgresCypherCompiler
from drivine.cypher.sql.sqlite_cypher_compiler import SqliteCypherCompiler

NODE_PROBE = re.compile(
    r"LEFT JOIN LATERAL \(\s*SELECT \* FROM g_node (\w+) WHERE \1\.nid = .* OFFSET 0\s*\) \1 ON TRUE"
)
EDGE_PROBE = re.compile(
    r"LEFT JOIN LATERAL \(\s*SELECT \* FROM g_edge (\w+) WHERE \1\.eid = .* OFFSET 0\s*\) \1 ON TRUE"
)
PLAIN = re.compile(r"LEFT JOIN g_(node|edge) \w+ ON")

STATEMENTS = [
    # A write's rows, read back for the statement after its loop.
    ("MATCH (n:CyLoop) FOREACH (x IN [1] | CREATE (n)-[r:CY_SELF]->(n) SET r.w = x)", False),
    ("MATCH ()-[r:CY_SELF]->() FOREACH (x IN [1] | SET r.w = x)", True),
    # A list of nodes or relationships unwound.
    ("MATCH (n:CyLoop) WITH collect(n) AS ns UNWIND ns AS m RETURN m.i", False),
    ("MATCH ()-[r:CY_SELF]->() WITH collect(r) AS rs UNWIND rs AS x RETURN x.w", True),
    # A list of nodes or relationships returned whole, and read item by item.
    ("MATCH (n:CyLoop) WITH collect(n) AS ns RETURN ns", False),
    ("MATCH ()-[r:CY_SELF]->() WITH collect(r) AS rs RETURN rs", True),
    ("MATCH (n:CyLoop) WITH collect(n) AS ns RETURN [m IN ns WHERE m.i > 1 | m.i]", False),
    ("MATCH ()-[r:CY_SELF]->() WITH collect(r) AS rs RETURN [x IN rs WHERE x.w > 1 | x.w]", True),
]
"""Each statement, with whether it reads relationships back by id: one for each place a row is joined to its table."""


def compiled(cypher: str) -> str:
    """The statement as Postgres runs it: the procedure a write compiles to, then the query."""
    it = PostgresCypherCompiler("g").compile(cypher)
    return f"{it.prelude or ''}\n{it.sql}"


@pytest.mark.parametrize(("cypher", "relationships"), STATEMENTS, ids=[it for it, _ in STATEMENTS])
def test_a_row_reads_its_node_or_relationship_back_by_a_probe_of_the_tables_key_and_by_no_plain_join(
    cypher: str, relationships: bool
):
    sql = compiled(cypher)
    assert (EDGE_PROBE if relationships else NODE_PROBE).search(sql), f"{cypher}:\n{sql}"
    assert not PLAIN.search(sql), f"{cypher}:\n{sql}"


def test_oracle_and_sqlite_join_a_row_to_its_table_as_they_did():
    cypher = "MATCH (n:CyLoop) WITH collect(n) AS ns UNWIND ns AS m RETURN m.i"
    for sql in (OracleCypherCompiler("g").compile(cypher).sql, SqliteCypherCompiler("g").compile(cypher).sql):
        assert not re.search(r"LATERAL \(\s*SELECT \* FROM", sql), sql
        assert re.search(r"LEFT JOIN \S*g_node\S* \w+ ON", sql, re.IGNORECASE), sql
