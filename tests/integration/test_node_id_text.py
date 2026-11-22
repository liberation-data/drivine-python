"""
A node found by its id's text on every store (#583, drivine4j#806): the text given to a load, a relate, load_related,
an unrelate, a patch or a delete is read as the NodeId field's type and bound as a save writes the field, so `'5'`
finds a node whose int id is 5 and `'2024-03-04'` one whose date id is that day, on Neo4j, Memgraph and FalkorDB as on
the SQL stores. Before, a graph engine bound the text as it was given, and found nothing. drivine4j's
NodeIdTextGraphCrossEngineTest, ported, run on every store the tests have.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any

import pytest

from drivine import (
    CascadeType,
    Direction,
    DrivineError,
    GraphView,
    NodeFragment,
    NodeId,
    PersistenceManager,
    Relationship,
    Root,
    StatelessGraphObjectManager,
    node_ref,
)


class TextLong(NodeFragment, labels=["IdText", "TextLong"]):
    id: Annotated[int, NodeId()]
    name: str | None = None


class TextDay(NodeFragment, labels=["IdText", "TextDay"]):
    id: Annotated[date, NodeId()]
    name: str | None = None


class TextKey(NodeFragment, labels=["IdText", "TextKey"]):
    id: Annotated[uuid.UUID, NodeId()]
    name: str | None = None


class TextString(NodeFragment, labels=["IdText", "TextString"]):
    id: Annotated[str, NodeId()]
    name: str | None = None


class TextStringAsLong(NodeFragment, labels=["IdText", "TextString"]):
    """TextString's labels with a number id: what a string id's text must not find by the same text."""

    id: Annotated[int, NodeId()]
    name: str | None = None


class TextTarget(NodeFragment, labels=["IdText", "TextTarget"]):
    id: Annotated[str, NodeId()]
    name: str | None = None


class TextDayTree(GraphView):
    """Days, each with the days that are part of it, one level deep: a cascade delete refuses a deeper tree."""

    day: Annotated[TextDay, Root()]
    parts: Annotated[list[TextDayTree], Relationship("PART_OF", Direction.INCOMING, max_depth=1)] = []


TARGET = node_ref(TextTarget, "x")


@pytest.fixture
async def gom(pm: PersistenceManager) -> StatelessGraphObjectManager:
    manager = StatelessGraphObjectManager(pm)
    await manager.save(TextTarget(id="x", name="the target"))
    return manager


async def round_trip(gom: StatelessGraphObjectManager, type_: type[Any], node: Any, text: str) -> None:
    """Saves `node`, then finds it by `text` in a load, a relate, load_related, an unrelate, a patch and a delete."""
    await gom.save(node)
    loaded = await gom.load(type_, text)
    assert loaded is not None and loaded.name == node.name
    assert await gom.edges.relate(node_ref(type_, text), TARGET, "POINTS_TO")
    related = await gom.edges.load_related(node_ref(type_, text), "POINTS_TO", TextTarget)
    assert [it.id for it in related] == ["x"]
    assert await gom.edges.unrelate(node_ref(type_, text), TARGET, "POINTS_TO") == 1
    assert await gom.edges.load_related(node_ref(type_, text), "POINTS_TO", TextTarget) == []
    patched = await gom.patch(type_, text, set={"name": "renamed"})
    assert (patched.found, patched.changed) == (True, True)
    assert await gom.delete(type_, text) == 1
    assert await gom.load(type_, node.id) is None


async def test_a_number_id_is_found_by_its_text(gom: StatelessGraphObjectManager):
    await round_trip(gom, TextLong, TextLong(id=5, name="five"), "5")


async def test_a_date_id_is_found_by_its_text(gom: StatelessGraphObjectManager):
    await round_trip(gom, TextDay, TextDay(id=date(2024, 3, 4), name="march"), "2024-03-04")


async def test_a_uuid_id_is_found_by_its_text(gom: StatelessGraphObjectManager):
    key = uuid.UUID("12345678-9abc-def0-1234-56789abcdef0")
    await round_trip(gom, TextKey, TextKey(id=key, name="key"), str(key))


async def test_a_string_id_is_found_by_its_text(gom: StatelessGraphObjectManager):
    await round_trip(gom, TextString, TextString(id="s5", name="string"), "s5")


async def test_unrelate_all_finds_its_node_by_the_ids_text(gom: StatelessGraphObjectManager):
    await gom.save(TextLong(id=9, name="nine"))
    assert await gom.edges.relate(node_ref(TextLong, 9), TARGET, "POINTS_TO")
    assert await gom.edges.unrelate_all(node_ref(TextLong, "9"), "POINTS_TO") == 1


async def test_a_cascade_delete_by_a_date_ids_text_checks_its_depth_and_deletes_the_tree(
    gom: StatelessGraphObjectManager,
):
    # The root is found as a load finds it, both in the check that refuses a tree deeper than the view reads and in the
    # delete itself.
    isos = ["2026-02-01", "2026-02-02", "2026-02-03"]
    for i, iso in enumerate(isos):
        await gom.save(TextDay(id=date.fromisoformat(iso), name=f"tree day {i}"))
    for i in range(1, len(isos)):
        assert await gom.edges.relate(node_ref(TextDay, isos[i]), node_ref(TextDay, isos[i - 1]), "PART_OF")
    with pytest.raises(DrivineError, match="1 node below that would be left behind"):
        await gom.delete(TextDayTree, "2026-02-01", cascade=CascadeType.DELETE_ALL)
    assert len(await gom.load_all(TextDay)) == 3
    assert await gom.delete(TextDay, "2026-02-03") == 1
    assert await gom.delete(TextDayTree, "2026-02-01", cascade=CascadeType.DELETE_ALL) == 2
    assert await gom.load_all(TextDay) == []


async def test_text_that_is_no_number_finds_nothing_on_a_number_id_and_raises_nothing(
    gom: StatelessGraphObjectManager,
):
    await gom.save(TextLong(id=7, name="seven"))
    assert await gom.load(TextLong, "abc") is None
    assert not await gom.edges.relate(node_ref(TextLong, "abc"), TARGET, "POINTS_TO")
    assert await gom.delete(TextLong, "abc") == 0
    assert await gom.load(TextLong, "7") is not None


async def test_text_on_a_string_id_finds_only_the_string_not_a_number_of_the_same_text(
    gom: StatelessGraphObjectManager,
):
    await gom.save(TextStringAsLong(id=6, name="number"))
    await gom.save(TextString(id="6", name="string"))
    found = await gom.load(TextString, "6")
    assert found is not None and found.name == "string"
