from collections.abc import AsyncIterator

import pytest

from drivine import DatabaseRegistry, PersistenceManager, PersistenceManagerFactory, QuerySpecification

ENGINES = ["default", "memgraph", "falkordb"]
"""The registered names of the databases each test runs on: Neo4j, Memgraph and FalkorDB."""


@pytest.fixture(scope="session")
async def registry() -> AsyncIterator[DatabaseRegistry]:
    registry = DatabaseRegistry()
    registry.build_or_resolve_from_env()
    registry.build_or_resolve_from_env("memgraph")
    registry.build_or_resolve_from_env("falkordb")
    yield registry
    await registry.close()


@pytest.fixture(scope="session")
def factory(registry: DatabaseRegistry) -> PersistenceManagerFactory:
    return PersistenceManagerFactory(registry)


@pytest.fixture(params=ENGINES)
async def pm(request: pytest.FixtureRequest, factory: PersistenceManagerFactory) -> AsyncIterator[PersistenceManager]:
    manager = factory.get(request.param)
    await manager.execute(QuerySpecification("MATCH (n) DETACH DELETE n"))
    yield manager


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """A test marked `rolls_back` needs multi-statement transactions, which FalkorDB doesn't have."""
    for item in items:
        callspec = getattr(item, "callspec", None)
        if item.get_closest_marker("rolls_back") and callspec is not None and callspec.params.get("pm") == "falkordb":
            item.add_marker(pytest.mark.skip(reason="FalkorDB commits each statement as it runs: nothing rolls back."))
