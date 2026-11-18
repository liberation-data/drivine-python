"""
A node whose id is a float, on each SQL store, found again by the text or the value the store holds the id as
(Drivine for TypeScript's StatelessGraphObjectManager.saveValues.spec.ts, drivine4j's SaveValuesCrossEngineTest): on
Oracle a binary double, which a lookup binds as extended JSON's double and reads `JSON($id EXTENDED)`; on Postgres the
text `jsonb` gives back, `numeric`'s; on SQLite the text the module writes. A whole id is the same on every store.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Any

import pytest

from drivine import (
    CaseInsensitive,
    GraphView,
    NodeFragment,
    NodeId,
    NodeStamp,
    PersistenceManager,
    QuerySpecification,
    Relationship,
    Root,
    StatelessGraphObjectManager,
    cypher_statement,
    node_ref,
)
from tests.integration.sql.sql_seed import SqlSeed


class SavedValue(NodeFragment, labels=["SavedValue"]):
    id: Annotated[str, NodeId()]


class SavedGauge(NodeFragment, labels=["SavedValue", "SavedGauge"]):
    """A node whose id is a float, which Oracle holds as a binary double."""

    id: Annotated[float, NodeId()]
    name: str


class SavedReadings(GraphView):
    """A node and the gauges it reads, which a save with `replace` unlinks by the text the store holds each id as."""

    reading: Annotated[SavedValue, Root()]
    gauges: Annotated[list[SavedGauge], Relationship("READS")] = []


@pytest.fixture(autouse=True)
async def cleared(pm: PersistenceManager) -> AsyncIterator[None]:
    seed = SqlSeed(pm, "SavedValue")
    await seed.clear()
    yield
    await seed.clear()


async def cypher(pm: PersistenceManager, statement: str, parameters: dict[str, Any] | None = None) -> list[Any]:
    return await pm.query(QuerySpecification[Any](cypher_statement(statement)).bind(parameters or {}))


async def test_a_node_whose_id_is_a_float_is_found_again_by_its_save_its_load_cypher_a_relate_and_a_delete(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    await gom.save(SavedValue(id="a"))
    ids = [4.5, 0.1, 123456.789]
    found: list[Any] = []
    for id in ids:
        await gom.save(SavedGauge(id=id, name="one"))
        await gom.save(SavedGauge(id=id, name="two"))
        loaded = await gom.load(SavedGauge, id)
        found.append(
            [
                id,
                loaded.name if loaded else None,
                await cypher(pm, "MATCH (n:SavedGauge) WHERE n.id = $id RETURN count(n)", {"id": id}),
                await gom.edges.relate(node_ref(SavedGauge, id), node_ref(SavedValue, "a"), "POINTS"),
                [it.id for it in await gom.edges.load_related(node_ref(SavedGauge, id), "POINTS", SavedValue)],
            ]
        )
    assert found == [[id, "two", [1], True, ["a"]] for id in ids]
    # One whose shortest text has an exponent, which each store spells its own way: Python's 1e-07, SQLite's 1.0E-7,
    # Postgres's jsonb 0.00000010, and Oracle a binary double. A save, a load, a relate, load_related and a delete each
    # find it as the store holds it.
    exponent = 1e-7
    await gom.save(SavedGauge(id=exponent, name="one"))
    await gom.save(SavedGauge(id=exponent, name="two"))
    assert await cypher(
        pm, "MATCH (n:SavedGauge) WHERE n.id = $id RETURN [count(n), collect(n.name)]", {"id": exponent}
    ) == [[1, ["two"]]]
    loaded = await gom.load(SavedGauge, exponent)
    assert [
        loaded.name if loaded else None,
        await gom.edges.relate(node_ref(SavedGauge, exponent), node_ref(SavedValue, "a"), "POINTS"),
        [it.id for it in await gom.edges.load_related(node_ref(SavedGauge, exponent), "POINTS", SavedValue)],
    ] == ["two", True, ["a"]]
    assert await gom.count(SavedGauge) == 4
    assert await gom.delete(SavedGauge, 4.5) == 1
    assert await gom.delete(SavedGauge, exponent) == 1
    assert await gom.count(SavedGauge) == 2


async def test_a_save_keeps_a_target_whose_id_is_a_float_it_holds_and_drops_one_it_does_not(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    # drivine4j's "a target whose id is a float with an exponent or a fraction is kept by a save that holds it": each
    # target's id is compared as the store holds it with the save's.
    ids = [1e20, 0.1, 1e-7, 123456.789, 1 / 3, 0.1 + 0.2]
    gauges = [SavedGauge(id=id, name=f"g{id}") for id in ids]
    await gom.save(SavedReadings(reading=SavedValue(id="r"), gauges=gauges))

    async def held() -> list[Any]:
        return await cypher(pm, "MATCH (:SavedValue {id: 'r'})-[:READS]->(g:SavedGauge) RETURN g.id ORDER BY g.id")

    loaded = await gom.load(SavedReadings, "r")
    assert loaded is not None
    await gom.save(loaded, replace=["gauges"])
    assert await held() == sorted(ids)
    loaded = await gom.load(SavedReadings, "r")
    assert loaded is not None
    loaded.gauges = [it for it in loaded.gauges if it.id != 1e-7]
    await gom.save(loaded, replace=["gauges"])
    assert await held() == sorted(it for it in ids if it != 1e-7)
    assert await gom.count(SavedGauge) == 6


async def test_patch_all_hands_each_node_whose_id_is_a_float_its_result_and_patches_each(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    # patch_all matches its results to its objects: on Oracle by the row's place, as the id the block gives back is its
    # plain JSON text of the binary double held, which need not read back as the number given.
    ids = [0.1 + 0.2, 1e-7, 123456.789, 4.5]
    await gom.save_all([SavedGauge(id=id, name="one") for id in ids])
    results = await gom.patch_all([SavedGauge(id=id, name=f"two {id}") for id in ids], only=["name"])
    assert [(it.found, it.applied, it.changed) for it in results] == [(True, True, True)] * len(ids)
    names: list[Any] = []
    for id in ids:
        loaded = await gom.load(SavedGauge, id)
        names.append(loaded.name if loaded else None)
    assert names == [f"two {id}" for id in ids]


async def test_a_patch_finds_a_node_whose_id_is_a_float_by_the_text_or_the_value_the_store_holds_the_id_as(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    # patch keys its node as a save does: Postgres's jsonb 0.00000010, SQLite's 1.0E-7, and Oracle a binary double.
    ids = [1e-7, 0.1 + 0.2, 4.5]
    await gom.save_all([SavedGauge(id=id, name="one") for id in ids])
    results = [await gom.patch(SavedGauge, id, set={"name": f"two {id}"}) for id in ids]
    assert [(it.found, it.applied, it.changed) for it in results] == [(True, True, True)] * len(ids)
    names: list[Any] = []
    for id in ids:
        loaded = await gom.load(SavedGauge, id)
        names.append(loaded.name if loaded else None)
    assert names == [f"two {id}" for id in ids]


class SavedReading(NodeFragment, labels=["SavedValue", "SavedReading"]):
    """A node with a case-insensitive property, whose lowercased copy Oracle writes back into what the node holds."""

    id: Annotated[str, NodeId()]
    name: Annotated[str, CaseInsensitive()]
    reading: float


async def test_a_whole_float_beside_a_case_insensitive_property_stays_a_float_through_a_save_and_a_patch(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    # On Oracle the lowercased copy is written by reading the node's properties and writing them back: as JSON, so a
    # binary double stays one, where their text would read back a whole one as a number.
    await gom.save(SavedReading(id="c", name="Ab", reading=2.0))
    assert await cypher(pm, "MATCH (n:SavedReading {id: 'c'}) RETURN toString(n.reading)") == ["2.0"]
    await gom.patch(SavedReading, "c", set={"name": "Cd", "reading": 3.0})
    assert await cypher(pm, "MATCH (n:SavedReading {id: 'c'}) RETURN [n.name, toString(n.reading)]") == [["Cd", "3.0"]]


class SavedCount(NodeFragment, labels=["SavedValue", "SavedCount"]):
    id: Annotated[int, NodeId()]
    name: str


class SavedFlag(NodeFragment, labels=["SavedValue", "SavedFlag"]):
    id: Annotated[bool, NodeId()]
    name: str


@pytest.mark.parametrize(("kind", "id"), [(SavedCount, 2**70), (SavedCount, 2**60 + 1), (SavedFlag, True)])
async def test_a_whole_number_past_a_long_and_a_boolean_id_are_found_by_a_load_a_patch_a_relate_and_a_delete(
    gom: StatelessGraphObjectManager, pm: PersistenceManager, kind: Any, id: Any
):
    # Each bound as the text the store holds it as, where a driver would bind it its own way: SQLite a boolean as 1,
    # and no whole number past a long at all.
    await gom.save(SavedValue(id="a"))
    await gom.save(kind(id=id, name="one"))
    await gom.save(kind(id=id, name="two"))
    loaded = await gom.load(kind, id)
    assert [loaded.name if loaded else None, await gom.count(kind)] == ["two", 1]
    assert (await gom.patch(kind, id, set={"name": "three"})).found
    assert await gom.edges.relate(node_ref(kind, id), node_ref(SavedValue, "a"), "POINTS")
    assert [it.id for it in await gom.edges.load_related(node_ref(kind, id), "POINTS", SavedValue)] == ["a"]
    assert await gom.delete(kind, id) == 1


class StampedGauge(NodeFragment, labels=["SavedValue", "StampedGauge"]):
    """A stamped node whose id is a float: a save_all hands each its stamp back by the id the store reads back."""

    id: Annotated[float, NodeId()]
    name: str
    stamp: Annotated[str | None, NodeStamp()] = None


async def test_save_all_hands_each_stamped_node_whose_id_is_a_float_its_stamp(gom: StatelessGraphObjectManager):
    # On Oracle a float id is held as a binary double, whose text need not read back as the number given (0.1 + 0.2):
    # the stamps a save_all reads back are matched to its objects by that id.
    saved = await gom.save_all([StampedGauge(id=0.1 + 0.2, name="a"), StampedGauge(id=1e-7, name="b")])
    assert all(it.stamp for it in saved)
    for it in saved:
        loaded = await gom.load(StampedGauge, it.id)
        assert loaded is not None and loaded.stamp == it.stamp
