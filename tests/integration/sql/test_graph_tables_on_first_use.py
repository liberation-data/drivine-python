"""
A SQL store's graph tables, made at its provider's first connection whatever is sent first: Cypher through a persistence
manager alone writes and reads on a new graph, the one the environment names, however many statements ask at once.
drivine4j#296, ported.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

import pytest

from drivine import (
    DatabaseRegistry,
    DatabaseType,
    NodeFragment,
    NodeId,
    PersistenceManagerFactory,
    QuerySpecification,
    StatelessGraphObjectManager,
    cypher_statement,
)
from tests.integration.sql.new_graph import (
    drop_graph,
    new_graph_registry,
    registry_on,
    store_objects_made,
    tables_made,
)


class FirstUse(NodeFragment, labels=["FirstUse"]):
    id: Annotated[str, NodeId()]
    name: str | None = None


def cypher(text: str, bindings: dict[str, Any] | None = None) -> QuerySpecification[Any]:
    return QuerySpecification[Any](cypher_statement(text)).bind(bindings)


@pytest.fixture
async def new_graph(store: str, factory: PersistenceManagerFactory) -> AsyncIterator[tuple[DatabaseRegistry, str]]:
    registry, name = new_graph_registry(store)
    try:
        yield registry, name
    finally:
        await registry.close()
        await drop_graph(factory.get(store), name)


async def test_cypher_through_a_persistence_manager_alone_writes_and_reads_on_a_new_graph(
    new_graph: tuple[DatabaseRegistry, str], factory: PersistenceManagerFactory, store: str
):
    registry, name = new_graph
    assert await tables_made(factory.get(store), name) == 0
    pm = PersistenceManagerFactory(registry).get(store)
    await pm.execute(
        cypher(
            "MERGE (a:Person {uuid: $a}) SET a.name = 'Ada' "
            "MERGE (b:Person {uuid: $b}) SET b.name = 'Grace' "
            "MERGE (a)-[:KNOWS {since: 2024}]->(b)",
            {"a": "p1", "b": "p2"},
        )
    )
    friends = await pm.query(
        cypher(
            "MATCH (:Person {name: $name})-[k:KNOWS]->(f:Person) RETURN {name: f.name, since: k.since}", {"name": "Ada"}
        )
    )
    assert friends == [{"name": "Grace", "since": 2024}]
    # In the graph the environment names, and in no other.
    assert await tables_made(factory.get(store), name) == 3
    made = await pm.get_one(QuerySpecification[Any](f"SELECT COUNT(*) AS made FROM {name}_node"))
    assert (made["made"] if isinstance(made, dict) else made) == 2
    # An object manager made after finds the tables made, and makes nothing more.
    assert [it.label for it in await StatelessGraphObjectManager(pm).indexes.list("RANGE")] == []


@pytest.mark.async_only
async def test_makes_a_new_graphs_tables_for_statements_that_ask_at_once(
    new_graph: tuple[DatabaseRegistry, str], store: str
):
    registry, _ = new_graph
    pm = PersistenceManagerFactory(registry).get(store)
    await asyncio.gather(*(pm.execute(cypher("CREATE (:Racer {n: $n})", {"n": i})) for i in range(6)))
    assert await pm.query(cypher("MATCH (r:Racer) RETURN count(r)")) == [6]


@pytest.mark.async_only
async def test_makes_the_tables_once_when_many_pools_and_object_managers_first_use_them_together_round_after_round(
    factory: PersistenceManagerFactory, store: str
):
    """
    Each pool makes the tables on its first connection while the others' first statements, compiled writes, and the
    object managers' own making run: on Oracle, DDL over a table that other sessions' writes hold could deadlock on its
    library cache lock (ORA-04020), and the first use failed (#287, liberation-data/drivine#454).
    """
    admin = factory.get(store)
    for _ in range(3):
        name = f"first_use_{uuid.uuid4().hex[:8]}"
        registries = [registry_on(store, name, pool_max=2) for _ in range(6)]
        try:
            managers = [PersistenceManagerFactory(it).get(store) for it in registries]
            written = [f"n{i}" for i in range(24)]
            saved = [f"om{i}" for i in range(3)]
            await asyncio.gather(
                *(
                    managers[i % len(managers)].execute(cypher("MERGE (:FirstUse {name: $name})", {"name": it}))
                    for i, it in enumerate(written)
                ),
                *(
                    StatelessGraphObjectManager(managers[2 * i]).save(FirstUse(id=it, name=it))
                    for i, it in enumerate(saved)
                ),
            )
            names = await managers[0].query(cypher("MATCH (n:FirstUse) RETURN n.name"))
            assert sorted(names) == sorted(written + saved)
            assert await store_objects_made(admin, name) == (4 if admin.type is DatabaseType.ORACLE else 3)
        finally:
            # Each round's pools are closed, and its graph dropped, failed or not, so the next tests have the store's
            # connections.
            await asyncio.gather(*(it.close() for it in registries))
            await drop_graph(admin, name)
