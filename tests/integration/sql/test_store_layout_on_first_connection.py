"""
The store layout's verdict on a graph, on each SQL store (liberation-data/drivine-cypher-spec#161), as a manager's
first connection takes it. A clear, `MATCH (n) DETACH DELETE n`, deletes Drivine's own nodes too, the store stamp among
them, as on a graph engine: a graph of this layout cleared and loaded again is stamped again on the next manager's first
connection, and used, not refused as one an earlier Drivine wrote. drivine4j's StoreLayoutCrossEngineTest, ported.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from drivine import (
    DatabaseRegistry,
    DatabaseType,
    PersistenceManager,
    PersistenceManagerFactory,
    QuerySpecification,
    StoreLayoutError,
    cypher_statement,
)
from drivine.backend.store_statements import ModuleStore, store_statements
from tests.integration.sql.new_graph import drop_graph, registry_on

MARKER = yaml.safe_load((Path(__file__).parents[2] / "cypher" / "marker.yaml").read_text())

Manager = Callable[[], PersistenceManager]


@pytest.fixture
async def graph(store: str, factory: PersistenceManagerFactory) -> AsyncIterator[tuple[str, Manager]]:
    """A graph of a new name, and what opens a new manager of it over a provider of its own, dropped after."""
    name = f"cy_layout_{store}"
    shared = factory.get(store)
    await drop_graph(shared, name)
    registries: list[DatabaseRegistry] = []

    def manager() -> PersistenceManager:
        registry = registry_on(store, name)
        registries.append(registry)
        return PersistenceManagerFactory(registry).get(store)

    try:
        yield name, manager
    finally:
        for registry in registries:
            await registry.close()
        await drop_graph(shared, name)


def cypher(text: str) -> QuerySpecification[Any]:
    return QuerySpecification[Any](cypher_statement(text))


async def stamps(pm: PersistenceManager, store: str, name: str) -> list[dict[str, Any]]:
    """The graph's stamps, `{id, assignedAt, layout}` each, as the module's statement reads them."""
    rows = await pm.query(QuerySpecification[Any](store_statements(cast(ModuleStore, store), name).stamp.read_stamps))
    found: list[dict[str, Any]] = []
    for row in rows:
        value = next(iter(row.values())) if isinstance(row, dict) and len(row) == 1 else row
        found.append(json.loads(value) if isinstance(value, str) else value)
    return found


async def table_there(pm: PersistenceManager, table: str) -> bool:
    if pm.type is DatabaseType.ORACLE:
        found = f"SELECT COUNT(*) AS n FROM user_tables WHERE table_name = UPPER('{table}')"
    elif pm.type is DatabaseType.SQLITE:
        found = f"SELECT COUNT(*) AS n FROM sqlite_master WHERE type = 'table' AND lower(name) = lower('{table}')"
    else:
        found = f"SELECT COUNT(*) AS n FROM pg_tables WHERE tablename = lower('{table}')"
    row = await pm.get_one(QuerySpecification[Any](found))
    return int(next(iter(row.values())) if isinstance(row, dict) else row) > 0


@pytest.mark.written_in_cypher
async def test_a_graph_cleared_and_loaded_again_is_stamped_again_on_the_next_first_connection_and_used(
    graph: tuple[str, Manager], store: str
):
    name, manager = graph
    first = manager()
    await first.execute(cypher("CREATE (:Person {name: 'Ada'})"))
    # The clear deletes the stamp with every node, and the values written after it have none.
    await first.execute(cypher("MATCH (n) DETACH DELETE n"))
    await first.execute(cypher("CREATE (:Person {name: 'Grace'})"))
    assert await stamps(first, store, name) == [], "the clear deleted the stamp"
    following = manager()
    assert await following.query(cypher("MATCH (p:Person) RETURN p.name")) == ["Grace"]
    [stamped] = await stamps(following, store, name)
    assert int(stamped["layout"]) == 2, stamped


@pytest.mark.written_in_cypher
async def test_an_empty_graph_stamped_with_an_earlier_layout_is_stamped_with_this_one_on_the_next_first_connection(
    graph: tuple[str, Manager], store: str
):
    name, manager = graph
    # A graph stamped as the clear above leaves one, its value then deleted and its stamp given the earlier layout.
    first = manager()
    await first.execute(cypher("CREATE (:Person {name: 'Grace'})"))
    await first.execute(cypher("MATCH (n) DETACH DELETE n"))
    await first.execute(cypher("CREATE (:Person {name: 'Grace'})"))
    stamped = manager()
    await stamped.execute(cypher("MATCH (p:Person) DETACH DELETE p"))
    await stamped.execute(cypher("MATCH (s:DrivineStore) SET s.layout = 1"))
    assert [int(it["layout"]) for it in await stamps(stamped, store, name)] == [1]
    following = manager()
    [restamped] = await stamps(following, store, name)
    assert int(restamped["layout"]) == 2, restamped
    await following.execute(cypher("CREATE (:Person {name: 'Ada'})"))
    assert await following.query(cypher("MATCH (p:Person) RETURN p.name")) == ["Ada"]


async def test_an_earlier_layouts_graph_with_no_stamp_is_refused_on_the_first_connection_and_nothing_is_made_in_it(
    graph: tuple[str, Manager], store: str, factory: PersistenceManagerFactory
):
    """marker.yaml's firstConnection cases on the store."""
    name, manager = graph
    shared = factory.get(store)
    cases = [it for it in MARKER["storeLayout"]["firstConnection"] if it["store"] == store]
    assert len(cases) == 1, f"marker.yaml's firstConnection cases on {store}"
    for case in cases:
        for statement in case["setup"]:
            await shared.execute(QuerySpecification[Any](statement.replace("{graph}", name)))
        with pytest.raises(StoreLayoutError) as refused:
            await manager().query(
                QuerySpecification[Any]("SELECT 1 AS one" + (" FROM dual" if store == "oracle" else ""))
            )
        assert str(refused.value) == case["refuse"].replace("{graph}", name), case["name"]
        assert refused.value.graph == name
        for absent in case["absent"]:
            assert not await table_there(shared, absent.replace("{graph}", name)), f"{case['name']}: {absent} was made"


@pytest.mark.written_in_cypher
async def test_a_graph_that_holds_values_and_no_stamp_and_none_of_this_layouts_objects_is_refused(
    graph: tuple[str, Manager], store: str, factory: PersistenceManagerFactory
):
    name, manager = graph
    first = manager()
    await first.execute(cypher("CREATE (:Person {name: 'Ada'})"))
    await first.execute(cypher("MATCH (n) DETACH DELETE n"))
    await first.execute(cypher("CREATE (:Person {name: 'Grace'})"))
    # As an earlier Drivine leaves a graph: no lock table, which only this layout makes, and no stamp.
    await factory.get(store).execute(QuerySpecification[Any](f"DROP TABLE {name}_lock2"))
    with pytest.raises(StoreLayoutError) as refused:
        await manager().query(cypher("MATCH (p:Person) RETURN p.name"))
    assert refused.value.graph == name
