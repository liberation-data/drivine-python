# Written by scripts/unasync.py from tests/integration/conftest.py:
# change that, and run the script again.
import os
from collections.abc import Iterator

import pytest

from drivine.backend.postgres.postgres_graph import PostgresGraph
from drivine.sync import (
    DatabaseRegistry,
    DatabaseType,
    PersistenceManager,
    PersistenceManagerFactory,
    QuerySpecification,
)
from tests.sync_integration.sql_cypher_fixtures import for_store

ORACLE = os.environ.get("ORACLE_DATABASE_TYPE") == "ORACLE"
"""Whether Oracle is configured: with ORACLE_TESTS=true."""

POSTGRES = os.environ.get("POSTGRES_DATABASE_TYPE") == "POSTGRES"
"""Whether Postgres is configured, as the tests' own environment configures it."""

ENGINES = ["default", "memgraph", "falkordb", *(["postgres"] if POSTGRES else []), *(["oracle"] if ORACLE else [])]
"""
The registered names of the databases each test runs on: Neo4j, Memgraph and FalkorDB, Postgres as the tests' own
environment configures it, and Oracle when set.
"""

SQL_STORES = ("postgres", "oracle")
"""The stores that keep a graph in SQL tables, and compile a Cypher statement to their SQL."""


@pytest.fixture(scope="session")
def registry() -> Iterator[DatabaseRegistry]:
    registry = DatabaseRegistry()
    registry.build_or_resolve_from_env()
    registry.build_or_resolve_from_env("memgraph")
    registry.build_or_resolve_from_env("falkordb")
    if POSTGRES:
        registry.build_or_resolve_from_env("postgres")
    if ORACLE:
        registry.build_or_resolve_from_env("oracle")
    yield registry
    registry.close()


@pytest.fixture(scope="session")
def factory(registry: DatabaseRegistry) -> PersistenceManagerFactory:
    return PersistenceManagerFactory(registry)


@pytest.fixture(params=ENGINES)
def pm(request: pytest.FixtureRequest, factory: PersistenceManagerFactory) -> Iterator[PersistenceManager]:
    manager = factory.get(request.param)
    if manager.type is DatabaseType.ORACLE:
        # A node's relationships go with it: the relationship table cascades.
        manager.execute(QuerySpecification("DELETE FROM drivine_node"))
    elif manager.type is DatabaseType.POSTGRES:
        # The store is made by an object manager, so it may not be there yet.
        manager.execute(QuerySpecification(PostgresGraph().ensuring))
        manager.execute(QuerySpecification("DELETE FROM drivine_node"))
    else:
        manager.execute(QuerySpecification("MATCH (n) DETACH DELETE n"))
    # A test written in Cypher sends its statements as they are: on a SQL store they are compiled, not taken for SQL.
    yield for_store(manager) if request.node.get_closest_marker("written_in_cypher") else manager


CYPHER_STORES = ["default", *(["postgres"] if POSTGRES else []), *(["oracle"] if ORACLE else [])]
"""
The registered names of the stores a Cypher statement is checked on, to give the same rows: Neo4j, and the SQL stores it
is compiled for, as the tests' environment has them.
"""


@pytest.fixture(params=CYPHER_STORES)
def cypher_pm(request: pytest.FixtureRequest, factory: PersistenceManagerFactory) -> PersistenceManager:
    """A manager of each store a Cypher statement is checked on: its graph is the test's to clear."""
    manager = factory.get(request.param)
    if manager.type is DatabaseType.POSTGRES:
        # The store is made by an object manager, so it may not be there yet.
        manager.execute(QuerySpecification(PostgresGraph().ensuring))
    return manager


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """
    A test marked `rolls_back` needs multi-statement transactions, which FalkorDB doesn't have. One marked
    `graph_engine_only` runs what a SQL store doesn't take, which the marker says. One marked `fails_on_sql_stores`
    fails on them for the issue it names, until that is fixed.
    """
    for item in items:
        callspec = getattr(item, "callspec", None)
        engine = callspec.params.get("pm") if callspec is not None else None
        if item.get_closest_marker("rolls_back") and engine == "falkordb":
            item.add_marker(pytest.mark.skip(reason="FalkorDB commits each statement as it runs: nothing rolls back."))
        graph_engine_only = item.get_closest_marker("graph_engine_only")
        if graph_engine_only and engine in SQL_STORES:
            item.add_marker(pytest.mark.skip(reason=f"Graph engines only: {graph_engine_only.args[0]}"))
        fails = item.get_closest_marker("fails_on_sql_stores")
        if fails and engine in SQL_STORES:
            item.add_marker(pytest.mark.xfail(reason=fails.args[0], strict=True))
