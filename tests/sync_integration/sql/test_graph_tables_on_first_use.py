# Written by scripts/unasync.py from tests/integration/sql/test_graph_tables_on_first_use.py:
# change that, and run the script again.
"""
A SQL store's graph tables, made at its provider's first connection whatever is sent first: Cypher through a persistence
manager alone writes and reads on a new graph, the one the environment names, however many statements ask at once.
drivine4j#296, ported.
"""

from collections.abc import Iterator
from typing import Annotated, Any

import pytest

from drivine.sync import (
    DatabaseRegistry,
    NodeFragment,
    NodeId,
    PersistenceManagerFactory,
    QuerySpecification,
    StatelessGraphObjectManager,
    cypher_statement,
)
from tests.sync_integration.sql.new_graph import (
    drop_graph,
    new_graph_registry,
    tables_made,
)


class FirstUse(NodeFragment, labels=["FirstUse"]):
    id: Annotated[str, NodeId()]
    name: str | None = None


def cypher(text: str, bindings: dict[str, Any] | None = None) -> QuerySpecification[Any]:
    return QuerySpecification[Any](cypher_statement(text)).bind(bindings)


@pytest.fixture
def new_graph(store: str, factory: PersistenceManagerFactory) -> Iterator[tuple[DatabaseRegistry, str]]:
    registry, name = new_graph_registry(store)
    try:
        yield registry, name
    finally:
        registry.close()
        drop_graph(factory.get(store), name)


def test_cypher_through_a_persistence_manager_alone_writes_and_reads_on_a_new_graph(
    new_graph: tuple[DatabaseRegistry, str], factory: PersistenceManagerFactory, store: str
):
    registry, name = new_graph
    assert tables_made(factory.get(store), name) == 0
    pm = PersistenceManagerFactory(registry).get(store)
    pm.execute(
        cypher(
            "MERGE (a:Person {uuid: $a}) SET a.name = 'Ada' "
            "MERGE (b:Person {uuid: $b}) SET b.name = 'Grace' "
            "MERGE (a)-[:KNOWS {since: 2024}]->(b)",
            {"a": "p1", "b": "p2"},
        )
    )
    friends = pm.query(
        cypher(
            "MATCH (:Person {name: $name})-[k:KNOWS]->(f:Person) RETURN {name: f.name, since: k.since}", {"name": "Ada"}
        )
    )
    assert friends == [{"name": "Grace", "since": 2024}]
    # In the graph the environment names, and in no other.
    assert tables_made(factory.get(store), name) == 3
    made = pm.get_one(QuerySpecification[Any](f"SELECT COUNT(*) AS made FROM {name}_node"))
    assert (made["made"] if isinstance(made, dict) else made) == 2
    # An object manager made after finds the tables made, and makes nothing more.
    assert [it.label for it in StatelessGraphObjectManager(pm).indexes.list("RANGE")] == []
