# Written by scripts/unasync.py from tests/integration/oracle/test_oracle_adaptive_plans.py:
# change that, and run the script again.
"""
Oracle makes no adaptive plan in a session of Drivine's. Once the graph's tables had grown and shrunk without
statistics, an adaptive plan for a join that filters a node by a label and a property gave no rows, where the same plan,
fixed, gave them all (#146, liberation-data/drivine#331). That state comes of a long run and isn't made here, so the
tests ask for the plan: in a session of Drivine's it is fixed, and in a session left as Oracle opens it the same read is
planned adaptive. Ported from drivine's OracleAdaptivePlans and drivine4j's OracleAdaptivePlansTest.
"""

import dataclasses
import textwrap
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from drivine.cypher.sql.oracle_cypher_compiler import OracleCypherCompiler
from drivine.query.statement import cypher_statement
from drivine.sync import (
    Connection,
    ConnectionProperties,
    DatabaseRegistry,
    PersistenceManagerFactory,
    QuerySpecification,
)
from drivine.sync.connection.oracle.oracle_connection_provider import OracleConnectionProvider
from tests.cypher.cypher_spec import load
from tests.sync_integration.oracle.oracle_seed import ON_ORACLE

pytestmark = ON_ORACLE

GRAPH = "drivine_adaptive_plans"

CYPHER = "MATCH (:Person {name: 'Keanu'})-[r:ACTED_IN]->(m) RETURN m.title"
"""The read that found no rows in the Cypher suite (liberation-data/drivine#331)."""

READ = OracleCypherCompiler(GRAPH).compile(CYPHER).sql


@pytest.fixture(scope="module")
def factory_of_graph(
    registry: DatabaseRegistry, factory: PersistenceManagerFactory
) -> Iterator[PersistenceManagerFactory]:
    """The spec's graph, written to tables of its own, which are dropped after."""
    if registry.registered("oracle_adaptive_plans") is None:
        properties = dataclasses.replace(ConnectionProperties.from_env("oracle"), default_graph_path=GRAPH)
        registry.builder().with_properties(properties).register("oracle_adaptive_plans")
    pm = factory.get("oracle_adaptive_plans")
    pm.execute(QuerySpecification[Any](f"DELETE FROM {GRAPH}_node"))
    graph = textwrap.dedent(load("queries.yaml").graph or "").strip()
    pm.query(QuerySpecification[Any](cypher_statement(graph)))
    yield factory
    for statement in [
        f"DROP PROPERTY GRAPH IF EXISTS {GRAPH}",
        f"DROP TABLE IF EXISTS {GRAPH}_edge PURGE",
        f"DROP TABLE IF EXISTS {GRAPH}_node PURGE",
        f"DROP TABLE IF EXISTS {GRAPH}_schema PURGE",
    ]:
        pm.execute(QuerySpecification[Any](statement))


@pytest.fixture
def left_alone() -> Iterator[OracleConnectionProvider]:
    """A provider of the graph whose sessions are left as Oracle opens them, as the application can ask."""
    properties = ConnectionProperties.from_env("oracle")
    provider = OracleConnectionProvider(
        "oracle_left_alone",
        f"{properties.host}:{properties.port or 1521}/{properties.database_name}",
        properties.user_name,
        properties.password,
        graph=GRAPH,
        sets_no_adaptive_plans=False,
    )
    yield provider
    provider.end()


def notes_of(connection: Connection, sql: str) -> str:
    """The notes of the plan Oracle makes for `sql` in the session of `connection`."""
    id = f"adaptive_{uuid.uuid4().hex[:8]}"
    connection.query(QuerySpecification[Any](f"EXPLAIN PLAN SET STATEMENT_ID = '{id}' FOR {sql}"))
    lines = connection.query(
        QuerySpecification[Any](
            f"SELECT plan_table_output FROM TABLE(DBMS_XPLAN.DISPLAY('PLAN_TABLE', '{id}', 'TYPICAL +ADAPTIVE +NOTE'))"
        )
    )
    return "\n".join(str(it) for it in lines)


def assert_fixed(notes: str) -> None:
    assert "Plan hash value" in notes
    assert "adaptive plan" not in notes, notes


def test_each_session_of_drivines_pool_plans_the_read_fixed(
    factory_of_graph: PersistenceManagerFactory, registry: DatabaseRegistry
):
    provider = registry.connection_provider("oracle_adaptive_plans")
    assert provider is not None
    # Two sessions at once, so each is one the pool opened.
    first = provider.connect()
    second = provider.connect()
    try:
        assert first.session_id != second.session_id
        assert_fixed(notes_of(first, READ))
        assert_fixed(notes_of(second, READ))
    finally:
        first.release()
        second.release()


def test_a_session_left_as_oracle_opens_it_plans_the_same_read_adaptive(
    factory_of_graph: PersistenceManagerFactory, left_alone: OracleConnectionProvider
):
    connection = left_alone.connect()
    try:
        assert "this is an adaptive plan" in notes_of(connection, READ)
    finally:
        connection.release()


def test_the_read_gives_its_rows(factory_of_graph: PersistenceManagerFactory):
    rows = factory_of_graph.get("oracle_adaptive_plans").query(QuerySpecification[Any](cypher_statement(CYPHER)))
    assert sorted(rows) == ["Johnny Mnemonic", "The Matrix", "The Matrix Reloaded"]
