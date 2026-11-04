"""
#396, liberation-data/drivine4j#576: a node equal to a node is their keys compared, through the store's own comparison
of two keys (`same_key`). On Oracle that is `+ 0` on one side, both ways round, so either is read by its key: compared
column to column, Oracle 23 dropped the later node's join and tested its label in a form that misses a node with more
than one label, so `MATCH (p)-[:KNOWS]->(q) MATCH (c:Person) WHERE c = q` gave no rows. drivine4j's NodeEqualityTest is
the reference; tests/integration/oracle/test_oracle_node_equality.py checks Oracle's plan.
"""

import re

import pytest

from drivine.backend.oracle.oracle_graph import holds_label as oracle_holds_label
from drivine.backend.sqlite.sqlite_graph import holds_label as sqlite_holds_label
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


# #404, liberation-data/drivine4j#578: on Oracle, a node with a label whose key a condition the row must pass sets
# equal to the key of a node found some other way tests its labels by their text (`keyed_labels`), which the index over
# labels can't answer, so Oracle reads it by its primary key. With the index's form, Oracle read that index again for
# each row of the other node, and `MATCH (a:Big) MATCH (b:Big) WHERE a = b` grew with the square of the label's nodes.
# The plans are pinned by tests/integration/oracle/test_oracle_keyed_label_plan.py, the rows by test_cypher_compiled.py.
# drivine4j's NodeEqualityTest, and drivine#606's review cases.


def oracle(statement: str, parameters: dict[str, object] | None = None) -> str:
    return OracleCypherCompiler("g").compile(statement, parameters or {}).sql


def compared(sql: str) -> tuple[str, str]:
    """The two nodes the SQL compares by their keys, both ways round, the earlier first, as their aliases are
    numbered."""
    m = re.search(r"\((n(\d+))\.nid = (n(\d+))\.nid \+ 0 AND n\d+\.nid = n\d+\.nid \+ 0\)", sql)
    assert m is not None, f"No node equal to a node in:\n{sql}"
    a, b = sorted([(m[1], int(m[2])), (m[3], int(m[4]))], key=lambda it: it[1])
    return a[0], b[0]


def assert_keyed_labels(statement: str, labels: tuple[str, ...] = ("Big",), earlier_scanned: bool = True) -> None:
    """That on Oracle the statement tests the labels of the later node it compares, `labels`, by their text, and those
    of the earlier through the index (where `earlier_scanned`)."""
    sql = oracle(statement, {"id": 5})
    a, b = compared(sql)
    where = f"{statement}\n{sql}"
    assert (f"JSON_EXISTS({a}.labels, " in sql) is earlier_scanned, where
    assert f"JSON_EXISTS({b}.labels, " not in sql, where
    for label in labels:
        assert oracle_holds_label(f"{b}.labels", label) in sql, where


def assert_scanned_labels(statement: str, labelled: int = 2) -> None:
    """That on Oracle the statement, which matches a label on each of `labelled` nodes, tests each through the index
    over labels, as before."""
    sql = oracle(statement)
    where = f"{statement}\n{sql}"
    assert len(re.findall(r"""JSON_EXISTS\(\w+\.labels, '\$\[\*]\?\(@ == "Big"\)'\)""", sql)) == labelled, where
    assert not any(oracle_holds_label(f"{it}.labels", "Big") in sql for it in re.findall(r"\bn\d+\b", sql)), where


@pytest.mark.parametrize(
    ("statement", "labels", "earlier_scanned"),
    [
        ("MATCH (a:Big) MATCH (b:Big) WHERE a = b RETURN count(*)", ("Big",), True),
        ("MATCH (a:Big) MATCH (b:Big) WHERE b = a RETURN count(*)", ("Big",), True),
        ("MATCH (a:Big) MATCH (b:Big) WHERE id(b) = id(a) RETURN count(*)", ("Big",), True),
        ("MATCH (a:Big) MATCH (b:Big) WHERE id(a) = id(b) RETURN count(*)", ("Big",), True),
        ("MATCH (a:Big), (b:Big) WHERE a = b RETURN count(*)", ("Big",), True),
        ("MATCH (a:Big) WHERE a.k = 1 MATCH (b:Big) WHERE a = b RETURN count(*)", ("Big",), True),
        ("MATCH (a:Big) MATCH (b:Big) WHERE a = b AND b.k > 0 RETURN count(*)", ("Big",), True),
        ("MATCH (a:Big) MATCH (b:Big) WHERE b.k > 0 AND (a = b AND b.k < 9) RETURN count(*)", ("Big",), True),
        ("MATCH (a:Big) OPTIONAL MATCH (b:Big) WHERE a = b RETURN count(b)", ("Big",), True),
        ("MATCH (a:Big) WHERE EXISTS { MATCH (b:Big) WHERE a = b } RETURN count(*)", ("Big",), True),
        ("MATCH (a:Big) RETURN COUNT { MATCH (b:Big) WHERE b = a }", ("Big",), True),
        ("MATCH (a:Big) RETURN [(x)-[:R]->(b:Big) WHERE b = a | b.k]", ("Big",), True),
        ("MATCH (a:Big) MATCH p = shortestPath((b:Big)-[*]-(c:Big)) WHERE b = a RETURN length(p)", ("Big",), True),
        ("MATCH (a:Big:Other) MATCH (b:Big:Other) WHERE a = b RETURN count(*)", ("Big", "Other"), True),
        # Each test of the later node's labels, where a pattern names it again.
        ("MATCH (a:Big) MATCH (b:Big) MATCH (b:Other) WHERE a = b RETURN count(*)", ("Big", "Other"), True),
        # The earlier node found by its properties, a relationship it ends, or its id, with no label.
        ("MATCH (a {k: 1}) MATCH (b:Big) WHERE a = b RETURN b", ("Big",), False),
        ("MATCH (x)-[:R]->(a) MATCH (b:Big) WHERE a = b RETURN count(*)", ("Big",), False),
        ("MATCH (a) WHERE id(a) = $id MATCH (b:Big) WHERE a = b RETURN b", ("Big",), False),
        ("MATCH (a) WHERE $id = id(a) MATCH (b:Big) WHERE a = b RETURN b", ("Big",), False),
        ("MATCH (a), (b:Big) WHERE id(a) IN [1, 2] AND id(b) = id(a) RETURN b", ("Big",), False),
    ],
)
def test_on_oracle_a_node_whose_key_is_set_equal_to_an_earlier_nodes_tests_its_labels_so_the_index_cant_answer_them(
    statement: str, labels: tuple[str, ...], earlier_scanned: bool
) -> None:
    assert_keyed_labels(statement, labels, earlier_scanned)


def test_on_oracle_the_earlier_node_is_read_by_its_key_where_one_found_by_its_properties_is_bound_after_it() -> None:
    later = oracle("MATCH (b:Big) MATCH (a {k: 1}) WHERE a = b RETURN b")
    assert oracle_holds_label("n1.labels", "Big") in later and "JSON_EXISTS(n1.labels" not in later, later


@pytest.mark.parametrize("test", ["a = b AND b = c", "c = b AND b = a"])
def test_on_oracle_each_node_after_the_first_of_a_chain_set_equal_is_read_by_its_key(test: str) -> None:
    chain = oracle(f"MATCH (a:Big) MATCH (b:Big) MATCH (c:Big) WHERE {test} RETURN count(*)")
    assert "JSON_EXISTS(n1.labels" in chain, chain
    assert "JSON_EXISTS(n2.labels" not in chain and "JSON_EXISTS(n3.labels" not in chain, chain
    for it in ("n2", "n3"):
        assert oracle_holds_label(f"{it}.labels", "Big") in chain, chain


@pytest.mark.parametrize(
    ("statement", "labelled"),
    [
        ("MATCH (a:Big) MATCH (b:Big) WHERE NOT a = b RETURN count(*)", 2),
        ("MATCH (a:Big) MATCH (b:Big) WHERE a = b OR b.k = 1 RETURN count(*)", 2),
        ("MATCH (a:Big) MATCH (b:Big) WHERE a = b XOR b.k = 1 RETURN count(*)", 2),
        ("MATCH (a:Big) MATCH (b:Big) WHERE (a = b) = true RETURN count(*)", 2),
        ("MATCH (a:Big) MATCH (b:Big) WHERE CASE WHEN a = b THEN true END RETURN count(*)", 2),
        ("MATCH (a:Big), (b:Big) RETURN count(CASE WHEN a = b THEN 1 END)", 2),
        # A node in a list is no pair of keys.
        ("MATCH (a:Big) MATCH (b:Big) WHERE b IN [a] RETURN count(*)", 2),
        # Found by nothing but the key: the index over labels is the way in, to b, and the earlier node is read by
        # its key.
        ("MATCH (a) MATCH (b:Big) WHERE a = b RETURN count(*)", 1),
        ("MATCH (a) WHERE EXISTS { MATCH (b:Big) WHERE a = b } RETURN count(*)", 1),
        # Nor does a subquery find the node around it, by its id or a relationship it ends there: it reads a's rows.
        ("MATCH (a) WHERE EXISTS { MATCH (b:Big) WHERE id(a) = 5 AND a = b } RETURN count(*)", 1),
        ("MATCH (a) WHERE EXISTS { MATCH (a)-[:R]->(x) } AND EXISTS { MATCH (b:Big) WHERE a = b } RETURN count(*)", 1),
        # Passed through a WITH, the earlier node is a column of that query, whose finding the condition doesn't see.
        ("MATCH (a:Big) WITH a MATCH (b:Big) WHERE a = b RETURN count(*)", 2),
        # Var-length and shortest path ends are no anchor.
        ("MATCH (x)-[*1..2]->(a) MATCH (b:Big) WHERE a = b RETURN count(*)", 1),
        # An aggregate's argument compiled first only to see whether it binds a value: what it finds by a label, in the
        # names it takes, doesn't make the node an argument compiled after it takes under the same name found by more
        # than its key. Each EXISTS is written twice, for its count to be of the values that aren't null.
        (
            "MATCH (z) RETURN count(EXISTS { MATCH (u) MATCH (v:Big) WHERE u = v }) AS c, "
            "count(EXISTS { MATCH (p:Big) }) AS d",
            4,
        ),
        # The same of an operation over aggregates, compiled first to see whether it binds a value in a subquery.
        (
            "MATCH (z) RETURN count(EXISTS { MATCH (u) MATCH (v:Big) WHERE u = v }) + "
            "count(EXISTS { MATCH (p:Big) }) AS c",
            4,
        ),
        # A node equal to itself is the only node there is.
        ("MATCH (a:Big) WHERE a = a RETURN count(*)", 1),
    ],
)
def test_on_oracle_a_node_equal_under_not_or_as_a_value_or_to_a_node_found_by_nothing_else_keeps_its_label_test(
    statement: str, labelled: int
) -> None:
    assert_scanned_labels(statement, labelled)


def test_on_postgres_and_sqlite_a_node_set_equal_to_a_node_tests_its_labels_as_any_nodes() -> None:
    statement = "MATCH (a:Big) MATCH (b:Big) WHERE a = b RETURN count(*)"
    postgres = sql_of(PostgresCypherCompiler, statement)
    assert "n1.labels @> '[\"Big\"]'" in postgres and "n2.labels @> '[\"Big\"]'" in postgres, postgres
    sqlite = sql_of(SqliteCypherCompiler, statement)
    assert sqlite_holds_label("n1.labels", "Big") in sqlite and sqlite_holds_label("n2.labels", "Big") in sqlite, sqlite
