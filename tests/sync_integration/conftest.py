# Written by scripts/unasync.py from tests/integration/conftest.py:
# change that, and run the script again.
import os
from collections.abc import Iterator

import pytest

from drivine.sync import (
    DatabaseRegistry,
    DatabaseType,
    PersistenceManager,
    PersistenceManagerFactory,
    QuerySpecification,
)

ORACLE = os.environ.get("ORACLE_DATABASE_TYPE") == "ORACLE"
"""Whether Oracle is configured: with ORACLE_TESTS=true."""

ENGINES = ["default", "memgraph", "falkordb", *(["oracle"] if ORACLE else [])]
"""The registered names of the databases each test runs on: Neo4j, Memgraph and FalkorDB, and Oracle when set."""


@pytest.fixture(scope="session")
def registry() -> Iterator[DatabaseRegistry]:
    registry = DatabaseRegistry()
    registry.build_or_resolve_from_env()
    registry.build_or_resolve_from_env("memgraph")
    registry.build_or_resolve_from_env("falkordb")
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
    else:
        manager.execute(QuerySpecification("MATCH (n) DETACH DELETE n"))
    yield manager


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """
    A test marked `rolls_back` needs multi-statement transactions, which FalkorDB doesn't have. One marked
    `graph_engines` is written in Cypher, which Oracle doesn't take.
    """
    for item in items:
        callspec = getattr(item, "callspec", None)
        engine = callspec.params.get("pm") if callspec is not None else None
        if item.get_closest_marker("rolls_back") and engine == "falkordb":
            item.add_marker(pytest.mark.skip(reason="FalkorDB commits each statement as it runs: nothing rolls back."))
        if item.get_closest_marker("graph_engines") and engine == "oracle":
            item.add_marker(pytest.mark.skip(reason="The test is written in Cypher, which Oracle doesn't take."))
