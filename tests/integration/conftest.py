from collections.abc import AsyncIterator

import pytest

from drivine import DatabaseRegistry, PersistenceManager, PersistenceManagerFactory, QuerySpecification

ENGINES = ["default", "memgraph"]
"""The registered names of the databases each test runs on: Neo4j, and Memgraph."""


@pytest.fixture(scope="session")
async def registry() -> AsyncIterator[DatabaseRegistry]:
    registry = DatabaseRegistry()
    registry.build_or_resolve_from_env()
    registry.build_or_resolve_from_env("memgraph")
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
