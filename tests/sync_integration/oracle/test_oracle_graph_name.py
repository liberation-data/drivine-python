# Written by scripts/unasync.py from tests/integration/oracle/test_oracle_graph_name.py:
# change that, and run the script again.
"""drivine4j's `default-graph-path`: a store on Oracle works on the graph its connection names, and on no other."""

import dataclasses
from collections.abc import Iterator
from typing import Annotated, Any

import pytest

from drivine.sync import (
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
from drivine.sync.manager.graph_object_manager_factory import GraphObjectManagerFactory
from tests.sync_integration.oracle.oracle_seed import ON_ORACLE

pytestmark = ON_ORACLE

GRAPH = "drivine_named_test"


class Tenant(NodeFragment, labels=["OraNamed"]):
    id: Annotated[str, NodeId()]
    name: str | None = None


def drop_graph(pm: PersistenceManager) -> None:
    for statement in [
        f"DROP PROPERTY GRAPH IF EXISTS {GRAPH}",
        f"DROP TABLE IF EXISTS {GRAPH}_edge PURGE",
        f"DROP TABLE IF EXISTS {GRAPH}_node PURGE",
        f"DROP TABLE IF EXISTS {GRAPH}_schema PURGE",
    ]:
        pm.execute(QuerySpecification[Any](statement))


@pytest.fixture(scope="module")
def managers(
    registry: DatabaseRegistry, factory: PersistenceManagerFactory
) -> Iterator[tuple[StatelessGraphObjectManager, StatelessGraphObjectManager]]:
    """The graph is dropped once, before the suite: its provider makes the tables on its first connection alone."""
    pm = factory.get("oracle")
    drop_graph(pm)
    if registry.registered("oracle_named") is None:
        properties = dataclasses.replace(ConnectionProperties.from_env("oracle"), default_graph_path=GRAPH)
        registry.builder().with_properties(properties).register("oracle_named")
    managers = GraphObjectManagerFactory(factory)
    named, unnamed = managers.get("oracle_named"), managers.get("oracle")
    yield named, unnamed
    unnamed.delete_all(Tenant)
    drop_graph(pm)


def count(pm: PersistenceManager, table: str, id: str) -> int:
    return pm.get_one(
        QuerySpecification[Any](
            f"SELECT COUNT(*) FROM {table} WHERE JSON_VALUE(props, '$.id') = $id "
            "AND JSON_EXISTS(labels, '$?(@ == \"OraNamed\")')"
        ).bind({"id": id})
    )


def test_makes_the_graphs_tables_and_saves_and_loads_there_alone(pm, managers):
    named, unnamed = managers
    named.save(Tenant(id="named-acme", name="Acme"))
    assert count(pm, f"{GRAPH}_node", "named-acme") == 1
    assert count(pm, "drivine_node", "named-acme") == 0
    loaded = named.load(Tenant, "named-acme")
    assert loaded is not None and loaded.name == "Acme"
    assert unnamed.load(Tenant, "named-acme") is None


def test_keeps_its_indexes_in_the_graphs_own_catalog(pm, managers):
    named, unnamed = managers
    spec = range_index("OraNamed", "name")
    assert (named.indexes.ensure(spec)).status == "CREATED"
    assert "OraNamed" in pm.query(QuerySpecification[Any](f"SELECT label FROM {GRAPH}_schema"))
    assert not any(it.label == "OraNamed" for it in unnamed.indexes.list("RANGE"))
    named.indexes.drop(spec)
