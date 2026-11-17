# Written by scripts/unasync.py from tests/integration/sql/test_float_ids.py:
# change that, and run the script again.
"""
A node whose id is a float, on each SQL store, found again by the text or the value the store holds the id as
(Drivine for TypeScript's StatelessGraphObjectManager.saveValues.spec.ts, drivine4j's SaveValuesCrossEngineTest): on
Oracle a binary double, which a lookup binds as extended JSON's double and reads `JSON($id EXTENDED)`; on Postgres the
text `jsonb` gives back, `numeric`'s; on SQLite the text the module writes. A whole id is the same on every store.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Any

import pytest

from drivine.sync import (
    GraphView,
    NodeFragment,
    NodeId,
    PersistenceManager,
    QuerySpecification,
    Relationship,
    Root,
    StatelessGraphObjectManager,
    cypher_statement,
    node_ref,
)
from tests.sync_integration.sql.sql_seed import SqlSeed


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
def cleared(pm: PersistenceManager) -> Iterator[None]:
    seed = SqlSeed(pm, "SavedValue")
    seed.clear()
    yield
    seed.clear()


def cypher(pm: PersistenceManager, statement: str, parameters: dict[str, Any] | None = None) -> list[Any]:
    return pm.query(QuerySpecification[Any](cypher_statement(statement)).bind(parameters or {}))


def test_a_node_whose_id_is_a_float_is_found_again_by_its_save_its_load_cypher_a_relate_and_a_delete(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    gom.save(SavedValue(id="a"))
    ids = [4.5, 0.1, 123456.789]
    found: list[Any] = []
    for id in ids:
        gom.save(SavedGauge(id=id, name="one"))
        gom.save(SavedGauge(id=id, name="two"))
        loaded = gom.load(SavedGauge, id)
        found.append(
            [
                id,
                loaded.name if loaded else None,
                cypher(pm, "MATCH (n:SavedGauge) WHERE n.id = $id RETURN count(n)", {"id": id}),
                gom.edges.relate(node_ref(SavedGauge, id), node_ref(SavedValue, "a"), "POINTS"),
                [it.id for it in gom.edges.load_related(node_ref(SavedGauge, id), "POINTS", SavedValue)],
            ]
        )
    assert found == [[id, "two", [1], True, ["a"]] for id in ids]
    # One whose shortest text has an exponent, which each store spells its own way: Python's 1e-07, SQLite's 1.0E-7,
    # Postgres's jsonb 0.00000010, and Oracle a binary double. A save, a load, a relate, load_related and a delete each
    # find it as the store holds it.
    exponent = 1e-7
    gom.save(SavedGauge(id=exponent, name="one"))
    gom.save(SavedGauge(id=exponent, name="two"))
    assert cypher(pm, "MATCH (n:SavedGauge) WHERE n.id = $id RETURN [count(n), collect(n.name)]", {"id": exponent}) == [
        [1, ["two"]]
    ]
    loaded = gom.load(SavedGauge, exponent)
    assert [
        loaded.name if loaded else None,
        gom.edges.relate(node_ref(SavedGauge, exponent), node_ref(SavedValue, "a"), "POINTS"),
        [it.id for it in gom.edges.load_related(node_ref(SavedGauge, exponent), "POINTS", SavedValue)],
    ] == ["two", True, ["a"]]
    assert gom.count(SavedGauge) == 4
    assert gom.delete(SavedGauge, 4.5) == 1
    assert gom.delete(SavedGauge, exponent) == 1
    assert gom.count(SavedGauge) == 2


def test_a_save_keeps_a_target_whose_id_is_a_float_it_holds_and_drops_one_it_does_not(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    # drivine4j's "a target whose id is a float with an exponent or a fraction is kept by a save that holds it": each
    # target's id is compared as the store holds it with the save's.
    ids = [1e20, 0.1, 1e-7, 123456.789, 1 / 3, 0.1 + 0.2]
    gauges = [SavedGauge(id=id, name=f"g{id}") for id in ids]
    gom.save(SavedReadings(reading=SavedValue(id="r"), gauges=gauges))

    def held() -> list[Any]:
        return cypher(pm, "MATCH (:SavedValue {id: 'r'})-[:READS]->(g:SavedGauge) RETURN g.id ORDER BY g.id")

    loaded = gom.load(SavedReadings, "r")
    assert loaded is not None
    gom.save(loaded, replace=["gauges"])
    assert held() == sorted(ids)
    loaded = gom.load(SavedReadings, "r")
    assert loaded is not None
    loaded.gauges = [it for it in loaded.gauges if it.id != 1e-7]
    gom.save(loaded, replace=["gauges"])
    assert held() == sorted(it for it in ids if it != 1e-7)
    assert gom.count(SavedGauge) == 6


def test_patch_all_hands_each_node_whose_id_is_a_float_its_result_and_patches_each(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    # patch_all matches its results to its objects: on Oracle by the row's place, as the id the block gives back is its
    # plain JSON text of the binary double held, which need not read back as the number given.
    ids = [0.1 + 0.2, 1e-7, 123456.789, 4.5]
    gom.save_all([SavedGauge(id=id, name="one") for id in ids])
    results = gom.patch_all([SavedGauge(id=id, name=f"two {id}") for id in ids], only=["name"])
    assert [(it.found, it.applied, it.changed) for it in results] == [(True, True, True)] * len(ids)
    names: list[Any] = []
    for id in ids:
        loaded = gom.load(SavedGauge, id)
        names.append(loaded.name if loaded else None)
    assert names == [f"two {id}" for id in ids]
