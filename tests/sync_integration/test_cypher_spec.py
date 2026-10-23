# Written by scripts/unasync.py from tests/integration/test_cypher_spec.py:
# change that, and run the script again.
"""
A Cypher statement written for a graph engine runs on a SQL store, compiled to its SQL, and gives the same rows: each
case of the Cypher spec (tests/cypher, see its README.md) is a test of its own on Neo4j, Postgres and Oracle.
queries.yaml reads the graph it writes, written again before each case; writes.yaml and temporal.yaml start from an
empty graph. refusals.yaml is compiled with no store, by tests/unit/test_cypher_refusals.py.
"""

import textwrap
from typing import Any

import pytest

from drivine.query.statement import cypher_statement
from drivine.sync import DatabaseType, PersistenceManager, QuerySpecification
from tests.cypher.cypher_spec import Case, check_rows, expanded, failure_matches, load

NAMES = {DatabaseType.NEO4J: "neo4j", DatabaseType.POSTGRES: "postgres", DatabaseType.ORACLE: "oracle"}
"""The name a step gives each store by."""

QUERIES = load("queries.yaml")
WRITES = load("writes.yaml")
TEMPORAL = load("temporal.yaml")


def query(pm: PersistenceManager, cypher: str, parameters: dict[str, Any] | None = None) -> list[Any]:
    statement = cypher_statement(textwrap.dedent(cypher).strip())
    return pm.query(QuerySpecification[Any](statement).bind(parameters))


def clear(pm: PersistenceManager) -> None:
    """
    Removes every node and relationship, as the other tests clear the graph: the spec's statements match its labels
    (`:Person`) where they don't name the one its nodes share (`:CyTest`), so another test's nodes would be found.
    """
    if pm.type is DatabaseType.NEO4J:
        pm.execute(QuerySpecification("MATCH (n) DETACH DELETE n"))
    else:
        pm.execute(QuerySpecification("DELETE FROM drivine_edge"))
        pm.execute(QuerySpecification("DELETE FROM drivine_node"))


def run(pm: PersistenceManager, case: Case) -> None:
    """Each step of the case that runs on the store, in turn: its rows checked, or its failure."""
    store = NAMES[pm.type]
    for index, step in enumerate(case.steps):
        if not step.runs_on(store):
            continue
        where = f"{case.name}, step {index + 1}, on {store}: {step.cypher.strip()}"
        parameters = expanded(step.parameters)
        if step.error is not None:
            with pytest.raises(Exception) as failed:  # noqa: PT011
                query(pm, step.cypher, parameters)
            assert failure_matches(step, failed.value), f"{where}\nexpected: {step.error}\nactual:   {failed.value}"
            continue
        check_rows(step, query(pm, step.cypher, parameters), where)


@pytest.mark.parametrize("case", QUERIES.cases, ids=[it.name for it in QUERIES.cases])
def test_queries(cypher_pm: PersistenceManager, case: Case):
    clear(cypher_pm)
    query(cypher_pm, QUERIES.graph or "")
    run(cypher_pm, case)


@pytest.mark.parametrize("case", WRITES.cases, ids=[it.name for it in WRITES.cases])
def test_writes(cypher_pm: PersistenceManager, case: Case):
    clear(cypher_pm)
    run(cypher_pm, case)


@pytest.mark.parametrize("case", TEMPORAL.cases, ids=[it.name for it in TEMPORAL.cases])
def test_temporal(cypher_pm: PersistenceManager, case: Case):
    clear(cypher_pm)
    run(cypher_pm, case)
