# Written by scripts/unasync.py from tests/integration/oracle/test_oracle_temporal_without_era.py:
# change that, and run the script again.
"""
The Cypher spec's temporal cases (tests/cypher/temporal.yaml) on an Oracle store without the functions that renumber a
year BC (see `OracleGraph.era`), as one an earlier version made, or whose user may not make a function, has: each
statement is compiled to renumber it inline, and gives the same rows (liberation-data/drivine#572). drivine4j's
CypherTemporalOracleWithoutEraTest of liberation-data/drivine4j#584.
"""

import dataclasses
from collections.abc import Iterator
from typing import Any

import pytest

from drivine.backend.oracle.oracle_graph import OracleGraph
from drivine.sync import (
    ConnectionProperties,
    DatabaseRegistry,
    PersistenceManager,
    PersistenceManagerFactory,
    QuerySpecification,
    cypher_statement,
)
from tests.cypher.cypher_spec import Case
from tests.sync_integration.oracle.oracle_seed import ON_ORACLE
from tests.sync_integration.oracle.test_oracle_nulls_equal import dropping
from tests.sync_integration.test_cypher_spec import TEMPORAL
from tests.sync_integration.test_cypher_spec import run as run_case

pytestmark = ON_ORACLE

GRAPH = "cy_no_era"
"""The graph the cases run on, whose era functions are dropped after its provider's first connection makes them."""

PLANNED_TOO_SLOWLY = "Temporal values from CALL"
"""
Renumbered inline, a statement of this case takes Oracle over a minute to plan, and its session keeps the PGA the plan
grew (#402): on CI the suite then ran out of PGA (ORA-04036). It is left out here: that statement's SQL without the
functions is the SQL before them, which tests/unit/oracle_era_inline pins (call5); one CALL statement still runs inline
below.
"""

CASES = [it for it in TEMPORAL.cases if it.name != PLANNED_TOO_SLOWLY]


@pytest.fixture(scope="module")
def without_era(registry: DatabaseRegistry, factory: PersistenceManagerFactory) -> Iterator[PersistenceManager]:
    """
    A manager of the graph: its provider makes the graph's functions on its first connection, and the era functions are
    dropped after it.
    """
    pm = factory.get("oracle")
    for statement in dropping(GRAPH):
        pm.execute(QuerySpecification[Any](statement))
    if registry.registered("oracle_no_era") is None:
        properties = dataclasses.replace(ConnectionProperties.from_env("oracle"), default_graph_path=GRAPH)
        registry.builder().with_properties(properties).register("oracle_no_era")
    manager = factory.get("oracle_no_era")
    manager.query(QuerySpecification[Any]("SELECT 1 FROM dual"))
    for function in (OracleGraph(GRAPH).era, OracleGraph(GRAPH).era_date):
        pm.execute(QuerySpecification[Any](f"DROP FUNCTION {function}"))
    yield manager
    # Its sessions keep the PGA their inline plans grew (#402): ended, they free it for the suites after.
    provider = registry._providers.pop("oracle_no_era", None)
    if provider is not None:
        provider.end()
    for statement in dropping(GRAPH):
        pm.execute(QuerySpecification[Any](statement))


def clear(pm: PersistenceManager) -> None:
    pm.execute(QuerySpecification[Any](f"DELETE FROM {GRAPH}_node"))


@pytest.mark.parametrize("case", CASES, ids=[it.name for it in CASES])
def test_temporal_without_era(without_era: PersistenceManager, case: Case):
    clear(without_era)
    run_case(without_era, "temporal.yaml", case)


def test_numbers_a_moment_returned_by_a_call_as_neo4j_does(without_era: PersistenceManager):
    clear(without_era)
    without_era.execute(
        QuerySpecification[Any](cypher_statement("CREATE (:CyTemporal {id: 'a', day: date('2024-03-15')})"))
    )
    assert without_era.query(
        QuerySpecification[Any](
            cypher_statement("MATCH (n:CyTemporal) CALL { WITH n RETURN n.day AS d } RETURN (d + duration('P1D')).day")
        )
    ) == [16]


def test_a_statement_that_would_call_the_era_functions_and_nulls_equal_is_compiled_to_call_only_nulls_equal(
    without_era: PersistenceManager,
):
    # The store keeps nulls_equal and not the era functions: a write that compares [null] with [1] gives null, as Cypher
    # does, and the year BC is renumbered inline.
    clear(without_era)
    assert without_era.query(
        QuerySpecification[Any](
            cypher_statement(
                "CREATE (n:CyTemporal {id: 'm', day: date('-0044-03-15') + duration('P1D')}) "
                "WITH n, [null] AS a, [1] AS b RETURN [coalesce(a = b, 'null'), toString(n.day)]"
            )
        )
    ) == [["null", "-0044-03-16"]]


def test_the_store_has_no_era_function_to_call(without_era: PersistenceManager):
    # So each case above that reads or writes a year that may be BC was compiled again to renumber it inline.
    names = ", ".join(f"'{it.upper()}'" for it in (OracleGraph(GRAPH).era, OracleGraph(GRAPH).era_date))
    assert (
        without_era.get_one(
            QuerySpecification[Any](f"SELECT COUNT(*) FROM user_objects WHERE object_name IN ({names})")
        )
        == 0
    )
