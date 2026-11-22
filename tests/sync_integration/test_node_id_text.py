# Written by scripts/unasync.py from tests/integration/test_node_id_text.py:
# change that, and run the script again.
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

from drivine.sync import (
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
    refs,
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
    other: uuid.UUID | None = None
    others: list[uuid.UUID] = []


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


class TextLongView(GraphView):
    long: Annotated[TextLong, Root()]
    targets: Annotated[list[TextTarget], Relationship("POINTS_TO")] = []


TARGET = node_ref(TextTarget, "x")


@pytest.fixture
def gom(pm: PersistenceManager) -> StatelessGraphObjectManager:
    manager = StatelessGraphObjectManager(pm)
    manager.save(TextTarget(id="x", name="the target"))
    return manager


def round_trip(gom: StatelessGraphObjectManager, type_: type[Any], node: Any, text: str) -> None:
    """Saves `node`, then finds it by `text` in a load, a relate, load_related, an unrelate, a patch and a delete."""
    gom.save(node)
    loaded = gom.load(type_, text)
    assert loaded is not None and loaded.name == node.name
    assert gom.edges.relate(node_ref(type_, text), TARGET, "POINTS_TO")
    related = gom.edges.load_related(node_ref(type_, text), "POINTS_TO", TextTarget)
    assert [it.id for it in related] == ["x"]
    assert gom.edges.unrelate(node_ref(type_, text), TARGET, "POINTS_TO") == 1
    assert gom.edges.load_related(node_ref(type_, text), "POINTS_TO", TextTarget) == []
    patched = gom.patch(type_, text, set={"name": "renamed"})
    assert (patched.found, patched.changed) == (True, True)
    assert gom.delete(type_, text) == 1
    assert gom.load(type_, node.id) is None


def test_a_number_id_is_found_by_its_text(gom: StatelessGraphObjectManager):
    round_trip(gom, TextLong, TextLong(id=5, name="five"), "5")


def test_a_date_id_is_found_by_its_text(gom: StatelessGraphObjectManager):
    round_trip(gom, TextDay, TextDay(id=date(2024, 3, 4), name="march"), "2024-03-04")


def test_a_uuid_id_is_found_by_its_text(gom: StatelessGraphObjectManager):
    key = uuid.UUID("12345678-9abc-def0-1234-56789abcdef0")
    round_trip(gom, TextKey, TextKey(id=key, name="key"), str(key))


def test_a_string_id_is_found_by_its_text(gom: StatelessGraphObjectManager):
    round_trip(gom, TextString, TextString(id="s5", name="string"), "s5")


def test_unrelate_all_finds_its_node_by_the_ids_text(gom: StatelessGraphObjectManager):
    gom.save(TextLong(id=9, name="nine"))
    assert gom.edges.relate(node_ref(TextLong, 9), TARGET, "POINTS_TO")
    assert gom.edges.unrelate_all(node_ref(TextLong, "9"), "POINTS_TO") == 1


def test_a_cascade_delete_by_a_date_ids_text_checks_its_depth_and_deletes_the_tree(
    gom: StatelessGraphObjectManager,
):
    # The root is found as a load finds it, both in the check that refuses a tree deeper than the view reads and in the
    # delete itself.
    isos = ["2026-02-01", "2026-02-02", "2026-02-03"]
    for i, iso in enumerate(isos):
        gom.save(TextDay(id=date.fromisoformat(iso), name=f"tree day {i}"))
    for i in range(1, len(isos)):
        assert gom.edges.relate(node_ref(TextDay, isos[i]), node_ref(TextDay, isos[i - 1]), "PART_OF")
    with pytest.raises(DrivineError, match="1 node below that would be left behind"):
        gom.delete(TextDayTree, "2026-02-01", cascade=CascadeType.DELETE_ALL)
    assert len(gom.load_all(TextDay)) == 3
    assert gom.delete(TextDay, "2026-02-03") == 1
    assert gom.delete(TextDayTree, "2026-02-01", cascade=CascadeType.DELETE_ALL) == 2
    assert gom.load_all(TextDay) == []


def test_text_that_is_no_number_finds_nothing_on_a_number_id_and_raises_nothing(
    gom: StatelessGraphObjectManager,
):
    gom.save(TextLong(id=7, name="seven"))
    assert gom.load(TextLong, "abc") is None
    assert not gom.edges.relate(node_ref(TextLong, "abc"), TARGET, "POINTS_TO")
    assert gom.delete(TextLong, "abc") == 0
    assert gom.load(TextLong, "7") is not None


def test_text_on_a_string_id_finds_only_the_string_not_a_number_of_the_same_text(
    gom: StatelessGraphObjectManager,
):
    gom.save(TextStringAsLong(id=6, name="number"))
    gom.save(TextString(id="6", name="string"))
    found = gom.load(TextString, "6")
    assert found is not None and found.name == "string"


def test_update_and_a_views_load_find_the_root_by_the_ids_text(gom: StatelessGraphObjectManager):
    gom.save(TextLong(id=11, name="eleven"))
    assert gom.edges.relate(node_ref(TextLong, 11), TARGET, "POINTS_TO")
    view = gom.load(TextLongView, "11")
    assert view is not None and [it.id for it in view.targets] == ["x"]
    updated = gom.update(TextLong, "11", lambda it: it.model_copy(update={"name": "renamed"}))
    assert updated is not None and updated.name == "renamed"
    loaded = gom.load(TextLong, 11)
    assert loaded is not None and loaded.name == "renamed"


def test_a_uuid_in_a_property_and_a_list_is_saved_patched_and_matched_and_reads_back_as_a_uuid(
    gom: StatelessGraphObjectManager,
):
    # Each graph engine holds a UUID as its text (#583): a save, a patch and a where clause's binding send it so, and a
    # field declared UUID reads the text back as the UUID.
    key, other, third = uuid.UUID(int=1), uuid.UUID(int=2), uuid.UUID(int=3)
    gom.save(TextKey(id=key, name="key", other=other, others=[other, third]))
    loaded = gom.load(TextKey, key)
    assert loaded is not None and (loaded.id, loaded.other, loaded.others) == (key, other, [other, third])
    assert isinstance(loaded.id, uuid.UUID) and isinstance(loaded.other, uuid.UUID)
    patched = gom.patch(TextKey, str(key), set={"other": third})
    assert (patched.found, patched.changed) == (True, True)
    q = refs(TextKey)
    found = gom.load_all(TextKey, where=q.other.eq(third))
    assert [it.id for it in found] == [key]
