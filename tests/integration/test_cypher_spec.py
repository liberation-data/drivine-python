"""
A Cypher statement written for a graph engine runs on a SQL store, compiled to its SQL, and gives the same rows: each
case of the Cypher spec (tests/cypher, see its README.md) is a test of its own on Neo4j, Postgres and Oracle.
queries.yaml reads the graph it writes, written again before each case; writes.yaml and temporal.yaml start from an
empty graph. refusals.yaml is compiled with no store, by tests/unit/test_cypher_refusals.py.
"""

import textwrap
from typing import Any

import pytest

from drivine import DatabaseType, PersistenceManager, QuerySpecification
from drivine.backend.cypher import quote_name
from drivine.query.statement import cypher_statement
from tests.cypher.cypher_spec import Case, check_rows, expanded, failure_matches, load

NAMES = {DatabaseType.NEO4J: "neo4j", DatabaseType.POSTGRES: "postgres", DatabaseType.ORACLE: "oracle"}
"""The name a step gives each store by."""

QUERIES = load("queries.yaml")
WRITES = load("writes.yaml")
TEMPORAL = load("temporal.yaml")


async def query(pm: PersistenceManager, cypher: str, parameters: dict[str, Any] | None = None) -> list[Any]:
    statement = cypher_statement(textwrap.dedent(cypher).strip())
    return await pm.query(QuerySpecification[Any](statement).bind(parameters))


RESET: set[int] = set()
"""The Neo4j managers whose constraints and indexes `clear` has dropped, once for the run: see `clear`."""


async def clear(pm: PersistenceManager) -> None:
    """
    Removes every node and relationship, as the other tests clear the graph: the spec's statements match its labels
    (`:Person`) where they don't name the one its nodes share (`:CyTest`), so another test's nodes would be found. On
    Neo4j, the first time, every constraint and index but its token lookups goes too, as drivine4j's
    `TestCleanup.resetGraph` empties the graph for its spec: Neo4j's `db.labels()` lists a label an index or a
    constraint names, as another test's vector index on `:SizedTest`, though no node has it.
    """
    if pm.type is DatabaseType.NEO4J:
        if id(pm) not in RESET:
            # Constraints first: a uniqueness constraint owns its index, which is not dropped while it holds it.
            for name in await pm.query(QuerySpecification[str]("SHOW CONSTRAINTS YIELD name RETURN name")):
                await pm.execute(QuerySpecification(f"DROP CONSTRAINT {quote_name(name)} IF EXISTS"))
            shown = "SHOW INDEXES YIELD name, type WHERE type <> 'LOOKUP' RETURN name"
            for name in await pm.query(QuerySpecification[str](shown)):
                await pm.execute(QuerySpecification(f"DROP INDEX {quote_name(name)} IF EXISTS"))
            RESET.add(id(pm))
        await pm.execute(QuerySpecification("MATCH (n) DETACH DELETE n"))
    else:
        await pm.execute(QuerySpecification("DELETE FROM drivine_edge"))
        await pm.execute(QuerySpecification("DELETE FROM drivine_node"))


async def run(pm: PersistenceManager, case: Case) -> None:
    """Each step of the case that runs on the store, in turn: its rows checked, or its failure."""
    store = NAMES[pm.type]
    for index, step in enumerate(case.steps):
        if not step.runs_on(store):
            continue
        where = f"{case.name}, step {index + 1}, on {store}: {step.cypher.strip()}"
        parameters = expanded(step.parameters)
        if step.error is not None:
            with pytest.raises(Exception) as failed:  # noqa: PT011
                await query(pm, step.cypher, parameters)
            assert failure_matches(step, failed.value), f"{where}\nexpected: {step.error}\nactual:   {failed.value}"
            continue
        check_rows(step, await query(pm, step.cypher, parameters), where)


@pytest.mark.parametrize("case", QUERIES.cases, ids=[it.name for it in QUERIES.cases])
async def test_queries(cypher_pm: PersistenceManager, case: Case):
    await clear(cypher_pm)
    await query(cypher_pm, QUERIES.graph or "")
    await run(cypher_pm, case)


@pytest.mark.parametrize("case", WRITES.cases, ids=[it.name for it in WRITES.cases])
async def test_writes(cypher_pm: PersistenceManager, case: Case):
    await clear(cypher_pm)
    await run(cypher_pm, case)


@pytest.mark.parametrize("case", TEMPORAL.cases, ids=[it.name for it in TEMPORAL.cases])
async def test_temporal(cypher_pm: PersistenceManager, case: Case):
    await clear(cypher_pm)
    await run(cypher_pm, case)
