"""drivine4j's `default-graph-path`: a store on Oracle works on the graph its connection names, and on no other."""

import dataclasses
from collections.abc import AsyncIterator
from typing import Annotated, Any

import pytest

from drivine import (
    ConnectionProperties,
    DatabaseRegistry,
    NodeFragment,
    NodeId,
    PersistenceManager,
    PersistenceManagerFactory,
    QuerySpecification,
    StatelessGraphObjectManager,
    range_index,
)
from drivine.manager.graph_object_manager_factory import GraphObjectManagerFactory
from tests.integration.oracle.oracle_seed import ON_ORACLE

pytestmark = ON_ORACLE

GRAPH = "drivine_named_test"


class Tenant(NodeFragment, labels=["OraNamed"]):
    id: Annotated[str, NodeId()]
    name: str | None = None


async def drop_graph(pm: PersistenceManager) -> None:
    for statement in [
        f"DROP PROPERTY GRAPH IF EXISTS {GRAPH}",
        f"DROP TABLE IF EXISTS {GRAPH}_edge PURGE",
        f"DROP TABLE IF EXISTS {GRAPH}_node PURGE",
        f"DROP TABLE IF EXISTS {GRAPH}_schema PURGE",
    ]:
        await pm.execute(QuerySpecification[Any](statement))


@pytest.fixture(scope="module")
async def managers(
    registry: DatabaseRegistry, factory: PersistenceManagerFactory
) -> AsyncIterator[tuple[StatelessGraphObjectManager, StatelessGraphObjectManager]]:
    """The graph is dropped once, before the suite: its provider makes the tables on its first connection alone."""
    pm = factory.get("oracle")
    await drop_graph(pm)
    if registry.registered("oracle_named") is None:
        properties = dataclasses.replace(ConnectionProperties.from_env("oracle"), default_graph_path=GRAPH)
        registry.builder().with_properties(properties).register("oracle_named")
    managers = GraphObjectManagerFactory(factory)
    named, unnamed = managers.get("oracle_named"), managers.get("oracle")
    yield named, unnamed
    await unnamed.delete_all(Tenant)
    await drop_graph(pm)


async def count(pm: PersistenceManager, table: str, id: str) -> int:
    return await pm.get_one(
        QuerySpecification[Any](
            f"SELECT COUNT(*) FROM {table} WHERE JSON_VALUE(props, '$.id') = $id "
            "AND JSON_EXISTS(labels, '$?(@ == \"OraNamed\")')"
        ).bind({"id": id})
    )


async def test_makes_the_graphs_tables_and_saves_and_loads_there_alone(pm, managers):
    named, unnamed = managers
    await named.save(Tenant(id="named-acme", name="Acme"))
    assert await count(pm, f"{GRAPH}_node", "named-acme") == 1
    assert await count(pm, "drivine_node", "named-acme") == 0
    loaded = await named.load(Tenant, "named-acme")
    assert loaded is not None and loaded.name == "Acme"
    assert await unnamed.load(Tenant, "named-acme") is None


async def test_keeps_its_indexes_in_the_graphs_own_catalog(pm, managers):
    named, unnamed = managers
    spec = range_index("OraNamed", "name")
    assert (await named.indexes.ensure(spec)).status == "CREATED"
    assert "OraNamed" in await pm.query(QuerySpecification[Any](f"SELECT label FROM {GRAPH}_schema"))
    assert not any(it.label == "OraNamed" for it in await unnamed.indexes.list("RANGE"))
    await named.indexes.drop(spec)
