# Written by scripts/unasync.py from tests/integration/oracle/test_oracle_keyed_label_plan.py:
# change that, and run the script again.
"""
#404, liberation-data/drivine4j#578: on Oracle, a node with a label whose key is set equal to a node found before it is
read by its primary key, once for each row of that node. With its label tested through the index over labels, Oracle
read that index again for each row, and filtered it on the key: `MATCH (a:Big) MATCH (b:Big) WHERE a = b` took 1.5 s
over 4,000 nodes, growing with their square. The plan's outline names each join Oracle eliminates, which #396 keeps it
from doing to the later node. drivine4j's OracleKeyedLabelPlanTest is the reference;
tests/unit/test_cypher_node_equality.py pins the SQL.
"""

import re
from collections.abc import Iterator
from typing import Any

import pytest

from drivine.cypher.sql.oracle_cypher_compiler import OracleCypherCompiler
from drivine.query.statement import cypher_statement
from drivine.sync import PersistenceManager, PersistenceManagerFactory, QuerySpecification
from tests.sync_integration.oracle.oracle_seed import ON_ORACLE, oracle_plan

GRAPH = "drivine"
NODES = 3000
SMALL = 750


def k(i: int) -> int | None:
    """A node's `k`, for its `i`: on half the nodes, 0 to 4."""
    return i % 5 if i % 2 == 0 else None


def counted(test: Any) -> int:
    """How many of the nodes, numbered from 1, the test holds of."""
    return sum(1 for i in range(1, NODES + 1) if test(i))


def query(pm: PersistenceManager, statement: str) -> list[Any]:
    return pm.query(QuerySpecification[Any](cypher_statement(statement)))


def clear(pm: PersistenceManager) -> None:
    pm.execute(QuerySpecification("DELETE FROM drivine_edge"))
    pm.execute(QuerySpecification("DELETE FROM drivine_node"))


@pytest.fixture(scope="module")
def seeded(factory: PersistenceManagerFactory) -> Iterator[None]:
    pm = factory.get("oracle")
    clear(pm)
    query(pm, f"UNWIND range(1, {NODES}) AS i CREATE (:Big:Other {{i: i, k: CASE WHEN i % 2 = 0 THEN i % 5 END}})")
    query(pm, f"UNWIND range(1, {SMALL}) AS i CREATE (:Small {{i: i}})")
    # A relationship into each Big node of odd i, from a Small node.
    query(pm, f"MATCH (b:Big) WHERE b.i % 2 = 1 MATCH (s:Small) WHERE s.i = b.i % {SMALL} + 1 CREATE (s)-[:R]->(b)")
    pm.execute(
        QuerySpecification(
            f"BEGIN DBMS_STATS.GATHER_TABLE_STATS(USER, '{GRAPH.upper()}_NODE'); "
            f"DBMS_STATS.GATHER_TABLE_STATS(USER, '{GRAPH.upper()}_EDGE'); END;"
        )
    )
    yield
    clear(pm)


pytestmark = [ON_ORACLE, pytest.mark.usefixtures("seeded")]


def assert_read_by_key(pm: PersistenceManager, statement: str, expected: int) -> None:
    """That the statement counts `expected`, and that Oracle reads `b`, the later of the two nodes compared, by its
    primary key, and keeps its join."""
    compiled = OracleCypherCompiler(GRAPH).compile(statement, {})
    compared = re.search(r"\b(n(\d+))\.nid = (n(\d+))\.nid \+ 0", compiled.sql)
    assert compared is not None, f"No node equal to a node in:\n{compiled.sql}"
    b = max([(compared[1], int(compared[2])), (compared[3], int(compared[4]))], key=lambda it: it[1])[0].upper()
    plan = oracle_plan(pm, compiled.sql, compiled.parameters, "BASIC +PREDICATE +OUTLINE")
    assert "INDEX UNIQUE SCAN" in plan and re.search(rf'access\("{b}"\."NID"=', plan), (
        f"{statement} does not read b by its key:\n{plan}"
    )
    assert "Outline Data" in plan, plan
    assert not re.search(rf'ELIMINATE_JOIN\([^)]*"{b}"', plan), f"{statement} eliminates b's join:\n{plan}"
    assert [int(it) for it in query(pm, statement)] == [expected], statement


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("MATCH (a:Big) MATCH (b:Big) WHERE a = b RETURN count(*)", NODES),
        ("MATCH (a:Big) MATCH (b:Big) WHERE b = a RETURN count(*)", NODES),
        ("MATCH (a:Big), (b:Big) WHERE a = b RETURN count(*)", NODES),
        ("MATCH (a:Big) WHERE a.k = 1 MATCH (b:Big) WHERE a = b RETURN count(*)", counted(lambda i: k(i) == 1)),
        ("MATCH (a:Big) MATCH (b:Big) WHERE a = b AND b.k > 0 RETURN count(*)", counted(lambda i: (k(i) or 0) > 0)),
        ("MATCH (a:Big:Other) MATCH (b:Big:Other) WHERE a = b RETURN count(*)", NODES),
        ("MATCH (x:Small)-[:R]->(a:Big) MATCH (b:Big) WHERE a = b RETURN count(*)", NODES // 2),
        ("MATCH (a:Big) MATCH (b:Big) WHERE id(b) = id(a) RETURN count(*)", NODES),
        ("MATCH (a:Big) WHERE EXISTS { MATCH (b:Big) WHERE a = b } RETURN count(*)", NODES),
    ],
)
def test_a_node_with_a_label_equal_to_a_node_found_by_its_label_properties_or_a_relationship_is_read_by_its_key(
    factory: PersistenceManagerFactory, statement: str, expected: int
):
    assert_read_by_key(factory.get("oracle"), statement, expected)
