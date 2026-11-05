# Written by scripts/unasync.py from tests/integration/oracle/test_oracle_node_equality.py:
# change that, and run the script again.
"""
#396, liberation-data/drivine4j#576: on Oracle, a node with a label equal to a node matched before it is still joined,
and so found when it has more than one label. Its key compared column to column, Oracle 23 eliminated the later node's
join and tested its label as the multivalue index's filter, row by row, which misses a node with more than one label.
The plan's outline names each join Oracle eliminates. drivine4j's OracleNodeEqualityPlanTest is the reference;
tests/unit/test_cypher_node_equality.py pins the SQL.
"""

import re
from collections.abc import Iterator
from typing import Any

import pytest

from drivine.cypher.sql.oracle_cypher_compiler import NO_FEEDBACK, OracleCypherCompiler
from drivine.query.statement import cypher_statement
from drivine.sync import PersistenceManager, PersistenceManagerFactory, QuerySpecification
from tests.sync_integration.oracle.oracle_seed import ON_ORACLE, oracle_plan

GRAPH = "drivine"


def clear(pm: PersistenceManager) -> None:
    pm.execute(QuerySpecification("DELETE FROM drivine_edge"))
    pm.execute(QuerySpecification("DELETE FROM drivine_node"))


@pytest.fixture(scope="module")
def seeded(factory: PersistenceManagerFactory) -> Iterator[None]:
    pm = factory.get("oracle")
    clear(pm)
    # Two labels on each node: Oracle's filter of the labels, in the multivalue index's form, missed such a node.
    pm.query(
        QuerySpecification[Any](
            cypher_statement(
                "CREATE (k:Person:CyEq {name: 'Keanu'}), (c:Person:CyEq {name: 'Carrie'}), "
                "(l:Person:CyEq {name: 'Laurence'}), (k)-[:KNOWS]->(c), (l)-[:KNOWS]->(k)"
            )
        )
    )
    yield
    clear(pm)


pytestmark = [ON_ORACLE, pytest.mark.usefixtures("seeded")]


def assert_joined(pm: PersistenceManager, statement: str, expected: list[Any]) -> None:
    """That the statement gives `expected`, and that Oracle keeps the join of `c`, the later of the two nodes
    compared."""
    compiled = OracleCypherCompiler(GRAPH).compile(statement, {})
    compared = re.search(r"\b(n(\d+))\.nid = (n(\d+))\.nid \+ 0", compiled.sql)
    assert compared is not None, f"No node equal to a node in:\n{compiled.sql}"
    later = max([(compared[1], int(compared[2])), (compared[3], int(compared[4]))], key=lambda it: it[1])[0].upper()
    plan = oracle_plan(pm, compiled.sql, compiled.parameters, "BASIC +OUTLINE")
    assert "Outline Data" in plan, plan
    assert not re.search(rf'ELIMINATE_JOIN\([^)]*"{later}"', plan), f"{statement} eliminates c's join:\n{plan}"
    assert pm.query(QuerySpecification[Any](cypher_statement(statement))) == expected, statement


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("MATCH (p {name: 'Keanu'})-[:KNOWS]->(q) MATCH (c:Person) WHERE c = q RETURN c.name", ["Carrie"]),
        ("MATCH (p {name: 'Keanu'})-[:KNOWS]->(q) MATCH (c:Person) WHERE q = c RETURN c.name", ["Carrie"]),
        ("MATCH (p {name: 'Keanu'})<-[:KNOWS]-(q) MATCH (c:Person) WHERE c = q RETURN c.name", ["Laurence"]),
        ("MATCH (p:Person {name: 'Keanu'})-[:KNOWS]->(q) MATCH (c:Person) WHERE c = q RETURN c.name", ["Carrie"]),
        ("MATCH (p {name: 'Keanu'})-[:KNOWS]->(q), (c:Person) WHERE c = q RETURN c.name", ["Carrie"]),
        ("MATCH (p {name: 'Keanu'})-[:KNOWS]->(q) MATCH (c:Person) WHERE id(c) = id(q) RETURN c.name", ["Carrie"]),
    ],
)
def test_a_node_with_two_labels_equal_to_a_node_matched_before_it_is_still_joined_and_found(
    factory: PersistenceManagerFactory, statement: str, expected: list[Any]
):
    assert_joined(factory.get("oracle"), statement, expected)


def test_a_node_read_by_its_key_in_a_statement_that_writes_is_found_and_written(
    factory: PersistenceManagerFactory,
):
    # A statement that writes is a procedure, and Oracle takes the step that reads c by its key with its hint (#415,
    # liberation-data/drivine4j#585).
    pm = factory.get("oracle")
    statement = "MATCH (p {name: 'Keanu'})-[:KNOWS]->(q) MATCH (c:Person) WHERE c = q SET c.seen = true RETURN c.name"
    sql = OracleCypherCompiler(GRAPH).compile(statement, {}).sql
    assert NO_FEEDBACK in sql, sql
    try:
        assert pm.query(QuerySpecification[Any](cypher_statement(statement))) == ["Carrie"], statement
        assert pm.query(
            QuerySpecification[Any](cypher_statement("MATCH (c:Person) WHERE c.seen = true RETURN c.name"))
        ) == ["Carrie"]
    finally:
        pm.query(QuerySpecification[Any](cypher_statement("MATCH (c:Person) REMOVE c.seen")))
