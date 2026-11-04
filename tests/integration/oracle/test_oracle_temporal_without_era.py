"""
The Cypher spec's temporal cases (tests/cypher/temporal.yaml) on an Oracle store without the functions that renumber a
year BC (see `OracleGraph.era`), as one an earlier version made, or whose user may not make a function, has: each
statement is compiled to renumber it inline, and gives the same rows (liberation-data/drivine#572). drivine4j's
CypherTemporalOracleWithoutEraTest of liberation-data/drivine4j#584.
"""

import dataclasses
from collections.abc import AsyncIterator
from typing import Any

import pytest

from drivine import (
    ConnectionProperties,
    DatabaseRegistry,
    PersistenceManager,
    PersistenceManagerFactory,
    QuerySpecification,
    cypher_statement,
)
from drivine.backend.oracle.oracle_graph import OracleGraph
from tests.cypher.cypher_spec import Case
from tests.integration.oracle.oracle_seed import ON_ORACLE
from tests.integration.oracle.test_oracle_nulls_equal import dropping
from tests.integration.test_cypher_spec import TEMPORAL
from tests.integration.test_cypher_spec import run as run_case

pytestmark = ON_ORACLE

GRAPH = "cy_no_era"
"""The graph the cases run on, whose era functions are dropped after its provider's first connection makes them."""


@pytest.fixture(scope="module")
async def without_era(
    registry: DatabaseRegistry, factory: PersistenceManagerFactory
) -> AsyncIterator[PersistenceManager]:
    """
    A manager of the graph: its provider makes the graph's functions on its first connection, and the era functions are
    dropped after it.
    """
    pm = factory.get("oracle")
    for statement in dropping(GRAPH):
        await pm.execute(QuerySpecification[Any](statement))
    if registry.registered("oracle_no_era") is None:
        properties = dataclasses.replace(ConnectionProperties.from_env("oracle"), default_graph_path=GRAPH)
        registry.builder().with_properties(properties).register("oracle_no_era")
    manager = factory.get("oracle_no_era")
    await manager.query(QuerySpecification[Any]("SELECT 1 FROM dual"))
    for function in (OracleGraph(GRAPH).era, OracleGraph(GRAPH).era_date):
        await pm.execute(QuerySpecification[Any](f"DROP FUNCTION {function}"))
    yield manager
    for statement in dropping(GRAPH):
        await pm.execute(QuerySpecification[Any](statement))


async def clear(pm: PersistenceManager) -> None:
    await pm.execute(QuerySpecification[Any](f"DELETE FROM {GRAPH}_node"))


@pytest.mark.parametrize("case", TEMPORAL.cases, ids=[it.name for it in TEMPORAL.cases])
async def test_temporal_without_era(without_era: PersistenceManager, case: Case):
    await clear(without_era)
    await run_case(without_era, "temporal.yaml", case)


async def test_a_statement_that_would_call_the_era_functions_and_nulls_equal_is_compiled_to_call_only_nulls_equal(
    without_era: PersistenceManager,
):
    # The store keeps nulls_equal and not the era functions: a write that compares [null] with [1] gives null, as Cypher
    # does, and the year BC is renumbered inline.
    await clear(without_era)
    assert await without_era.query(
        QuerySpecification[Any](
            cypher_statement(
                "CREATE (n:CyTemporal {id: 'm', day: date('-0044-03-15') + duration('P1D')}) "
                "WITH n, [null] AS a, [1] AS b RETURN [coalesce(a = b, 'null'), toString(n.day)]"
            )
        )
    ) == [["null", "-0044-03-16"]]


async def test_the_store_has_no_era_function_to_call(without_era: PersistenceManager):
    # So each case above that reads or writes a year that may be BC was compiled again to renumber it inline.
    names = ", ".join(f"'{it.upper()}'" for it in (OracleGraph(GRAPH).era, OracleGraph(GRAPH).era_date))
    assert (
        await without_era.get_one(
            QuerySpecification[Any](f"SELECT COUNT(*) FROM user_objects WHERE object_name IN ({names})")
        )
        == 0
    )
