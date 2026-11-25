"""
A node found by its id on each SQL store, under the store layout 2 (liberation-data/drivine-cypher-spec#245): Drivine
for TypeScript's SqlNodeIdLookup.spec.ts, and drivine4j's NodeIdLookupCrossEngineTest. A save, a load, a delete by id,
load_related, a relate and a patch compare the id as the store's index is made over it, the property's JSON on Postgres
and its class and key on Oracle and SQLite. So a temporal id, held as its tag, is found: a date id saved twice is one
node (#519). An id given as text is read as the NodeId field's type first; text that is no value of it finds nothing,
and a str id is the text given.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any

import pytest

from drivine import (
    CascadeType,
    DatabaseType,
    Direction,
    DrivineError,
    GraphProperty,
    GraphView,
    NodeFragment,
    NodeId,
    NodeStamp,
    PersistenceManager,
    QuerySpecification,
    RelateMode,
    Relationship,
    RemovedTargets,
    Root,
    StatelessGraphObjectManager,
    cypher_statement,
    node_ref,
    range_index,
    refs,
    transaction,
    uniqueness_constraint,
)
from drivine.backend.matched_node import MatchedNode
from drivine.backend.sql.sql_graph import sql_text
from drivine.backend.sql.sql_json import to_json
from drivine.backend.sql.value_layout import write
from drivine.model.model_registry import ModelRegistry
from tests.integration.sql.sql_seed import SqlSeed
from tests.integration.sql.test_schema_ddl import messages


class LookupNumber(NodeFragment, labels=["SqlLookup", "LookupNumber"]):
    id: Annotated[int, NodeId()]
    name: str | None = None


class LookupDate(NodeFragment, labels=["SqlLookup", "LookupDate"]):
    id: Annotated[date, NodeId()]
    name: str | None = None
    stamp: Annotated[str | None, NodeStamp()] = None


class LookupText(NodeFragment, labels=["SqlLookup", "LookupText"]):
    id: Annotated[str, NodeId()]
    name: str | None = None


class LookupTextNumber(NodeFragment, labels=["SqlLookup", "LookupText"]):
    """A node of LookupText's labels whose id is a number: what a text id must not find by the same text."""

    id: Annotated[int, NodeId()]
    name: str | None = None


class LookupTarget(NodeFragment, labels=["SqlLookup", "LookupTarget"]):
    id: Annotated[str, NodeId()]
    name: str | None = None


class LookupPriced(NodeFragment, labels=["SqlLookup", "LookupPriced"]):
    """A `$`-named NodeId, which the store layout holds under `$$code`."""

    code: Annotated[str, NodeId(), GraphProperty("$code")]
    name: str | None = None
    stamp: Annotated[str | None, NodeStamp()] = None


class LookupFloat(NodeFragment, labels=["SqlLookup", "LookupFloat"]):
    """A float NodeId, which Oracle holds as a binary double, and a write block finds as one (drivine#754)."""

    id: Annotated[float, NodeId()]
    name: str | None = None
    stamp: Annotated[str | None, NodeStamp()] = None


class LookupTargetDays(GraphView):
    """The target joined to days, which a save that replaces the list unlinks by their ids."""

    target: Annotated[LookupTarget, Root()]
    days: Annotated[list[LookupDate], Relationship("ON_DAY")] = []


class LookupKey(NodeFragment, labels=["SqlLookup", "LookupKey"]):
    id: Annotated[uuid.UUID, NodeId()]
    name: str | None = None
    stamp: Annotated[str | None, NodeStamp()] = None


class LookupTargetKeys(GraphView):
    """The target joined to keys of a UUID id, which a save that replaces the list unlinks by their ids."""

    target: Annotated[LookupTarget, Root()]
    keys: Annotated[list[LookupKey], Relationship("HAS_KEY")] = []


class LookupDateTree(GraphView):
    """Days, each with the days that are part of it, one level deep: a cascade delete refuses a deeper tree."""

    day: Annotated[LookupDate, Root()]
    parts: Annotated[list[LookupDateTree], Relationship("DAY_PART_OF", Direction.INCOMING, max_depth=1)] = []


TARGET = node_ref(LookupTarget, "x")


@pytest.fixture(autouse=True)
async def cleared(gom: StatelessGraphObjectManager, pm: PersistenceManager) -> AsyncIterator[None]:
    seed = SqlSeed(pm, "SqlLookup")
    await seed.clear()
    await gom.save(LookupTarget(id="x", name="the target"))
    yield
    await seed.clear()


async def names(gom: StatelessGraphObjectManager, type_: type[Any]) -> list[str]:
    """The names of the nodes of `type_`'s labels, in order."""
    return sorted(it.name for it in await gom.load_all(type_))


# ----- A temporal id -----


async def test_a_date_id_saved_twice_is_one_node_loaded_by_the_ids_text(gom: StatelessGraphObjectManager):
    await gom.save(LookupDate(id=date(2024, 1, 2), name="first"))
    await gom.save(LookupDate(id=date(2024, 1, 2), name="second"))
    assert await names(gom, LookupDate) == ["second"]
    loaded = await gom.load(LookupDate, "2024-01-02")
    assert loaded is not None and loaded.name == "second"


async def test_a_date_id_saved_twice_in_one_save_all_is_one_node(gom: StatelessGraphObjectManager):
    await gom.save_all(
        [LookupDate(id=date(2024, 1, 3), name="batch first"), LookupDate(id=date(2024, 1, 3), name="batch second")]
    )
    assert await names(gom, LookupDate) == ["batch second"]
    loaded = await gom.load(LookupDate, date(2024, 1, 3))
    assert loaded is not None and loaded.id == date(2024, 1, 3)


# ----- Load, delete by id and load_related, for a number, a date and a str id -----


async def lookups(gom: StatelessGraphObjectManager, type_: type[Any], node: Any, text: str) -> None:
    """Saves `node`, joins it to the target, and finds it by `text` through a load, load_related and a delete."""
    await gom.save(node)
    assert await gom.edges.relate(node_ref(type_, text), TARGET, "POINTS_TO")
    loaded = await gom.load(type_, text)
    assert loaded is not None and loaded.name == node.name
    related = await gom.edges.load_related(node_ref(type_, text), "POINTS_TO", LookupTarget)
    assert [it.id for it in related] == ["x"]
    assert await gom.delete(type_, text) == 1
    assert await gom.load(type_, text) is None


async def test_a_number_id_is_found_by_its_text(gom: StatelessGraphObjectManager):
    await lookups(gom, LookupNumber, LookupNumber(id=5, name="five"), "5")


async def test_a_date_id_is_found_by_its_text(gom: StatelessGraphObjectManager):
    await lookups(gom, LookupDate, LookupDate(id=date(2024, 3, 4), name="march"), "2024-03-04")


async def test_a_str_id_is_found_by_its_text(gom: StatelessGraphObjectManager):
    await lookups(gom, LookupText, LookupText(id="t5", name="text"), "t5")


# ----- Relate (a `ref` step) and patch (a `patch` step), each finding its node by the id's JSON -----


async def refs_and_patches(gom: StatelessGraphObjectManager, type_: type[Any], node: Any, text: str, absent: str):
    """
    Joins `node`, saved, to the target by refs given the id as text and as its value; patches its name; and refers to
    a node of the class that isn't there, `absent`, which is not made, and joins nothing.
    """
    await gom.save(node)
    id_ = node.id
    assert await gom.edges.relate(node_ref(type_, text), TARGET, "REFERS_TO")
    assert await gom.edges.relate(node_ref(type_, id_), TARGET, "REFERS_TO", {}, RelateMode.CREATE)
    assert await gom.edges.unrelate(node_ref(type_, text), TARGET, "REFERS_TO") == 2
    assert await gom.edges.relate(node_ref(type_, id_), TARGET, "REFERS_TO")

    patched = await gom.patch(type_, text, set={"name": "renamed"})
    assert (patched.found, patched.applied, patched.changed) == (True, True, True)
    loaded = await gom.load(type_, text)
    assert loaded is not None and loaded.name == "renamed"
    [all_] = await gom.patch_all([type_(id=id_, name="patched all")], only=["name"])
    assert (all_.found, all_.applied, all_.changed) == (True, True, True)
    loaded = await gom.load(type_, id_)
    assert loaded is not None and loaded.name == "patched all"

    assert not await gom.edges.relate(node_ref(type_, absent), TARGET, "REFERS_TO")
    assert not await gom.edges.relate(TARGET, node_ref(type_, absent), "REFERS_TO")
    assert await gom.edges.load_related(TARGET, "REFERS_TO", type_) == []
    assert await gom.edges.unrelate_all(node_ref(type_, text), "REFERS_TO") == 1


async def test_a_node_of_a_number_id_is_related_and_patched(gom: StatelessGraphObjectManager):
    await refs_and_patches(gom, LookupNumber, LookupNumber(id=21, name="a"), "21", "22")


async def test_a_node_of_a_date_id_is_related_and_patched(gom: StatelessGraphObjectManager):
    await refs_and_patches(gom, LookupDate, LookupDate(id=date(2024, 5, 6), name="a"), "2024-05-06", "2024-05-07")


async def test_a_node_of_a_str_id_is_related_and_patched(gom: StatelessGraphObjectManager):
    await refs_and_patches(gom, LookupText, LookupText(id="t21", name="a"), "t21", "t22")


async def test_a_node_of_a_float_id_is_related_and_patched(gom: StatelessGraphObjectManager):
    # Oracle holds a float as a binary double: each write block finds it by the id bound as one, read EXTENDED (#754).
    await refs_and_patches(gom, LookupFloat, LookupFloat(id=2.5, name="a"), "2.5", "3.5")


async def test_a_node_of_a_dollar_named_id_is_related_unrelated_patched_all_and_unrelated_all(
    gom: StatelessGraphObjectManager,
):
    await gom.save(LookupPriced(code="p2", name="a"))
    priced = node_ref(LookupPriced, "p2")
    assert await gom.edges.relate(priced, TARGET, "REFERS_TO", {}, RelateMode.CREATE)
    assert await gom.edges.relate(TARGET, priced, "REFERS_TO")
    assert await gom.edges.unrelate(priced, TARGET, "REFERS_TO") == 1
    [all_] = await gom.patch_all([LookupPriced(code="p2", name="patched all")], only=["name"])
    assert (all_.found, all_.applied, all_.changed) == (True, True, True)
    loaded = await gom.load(LookupPriced, "p2")
    assert loaded is not None and loaded.name == "patched all"
    assert await gom.edges.unrelate_all(priced, "REFERS_TO", Direction.INCOMING) == 1
    assert await gom.edges.load_related(TARGET, "REFERS_TO", LookupPriced) == []


async def test_each_node_of_a_save_all_is_found_by_a_float_a_date_and_a_dollar_named_id_and_handed_its_stamp(
    gom: StatelessGraphObjectManager,
):
    # A save_all writes a batch: each node found by its id, so a second save_all makes no second node, and each node's
    # stamp read back for it by its id, before and after the writes: a node the batch made is handed its whole stamp,
    # and one that was there, whose relationships the object didn't come from, its data token.
    def batch(suffix: str) -> list[Any]:
        return [
            LookupFloat(id=0.1, name=f"float{suffix}"),
            LookupDate(id=date(2024, 7, 8), name=f"date{suffix}"),
            LookupPriced(code="p3", name=f"priced{suffix}"),
        ]

    async def loaded() -> list[str | None]:
        found = [
            await gom.load(LookupFloat, 0.1),
            await gom.load(LookupDate, "2024-07-08"),
            await gom.load(LookupPriced, "p3"),
        ]
        return [it.stamp if it is not None else None for it in found]

    made = batch("")
    await gom.save_all(made)
    first = await loaded()
    assert all(it is not None and ":" in it for it in first)
    assert [it.stamp for it in made] == first

    again = batch(" again")
    await gom.save_all(again)
    assert await names(gom, LookupFloat) == ["float again"]
    assert await names(gom, LookupDate) == ["date again"]
    assert await names(gom, LookupPriced) == ["priced again"]
    assert [it.stamp for it in again] == [f"{(it or '').split(':')[0]}:" for it in await loaded()]


async def test_a_views_related_nodes_of_a_date_id_are_handed_their_stamps(gom: StatelessGraphObjectManager):
    days = LookupTarget(id="stamped days", name="a target of stamped days")
    first = LookupDate(id=date(2025, 3, 1), name="first day")
    second = LookupDate(id=date(2025, 3, 2), name="second day")
    await gom.save(LookupTargetDays(target=days, days=[first, second]))
    for it, iso in ((first, "2025-03-01"), (second, "2025-03-02")):
        loaded = await gom.load(LookupDate, iso)
        assert loaded is not None and loaded.stamp is not None
        assert it.stamp == loaded.stamp


async def test_a_target_of_a_date_id_is_unlinked_by_its_tags_text_and_the_one_still_held_kept(
    gom: StatelessGraphObjectManager,
):
    # A save that replaces a list keeps the targets the object still holds, told by their ids as the plan keys them,
    # and unlinks the rest: a target of a date id is read back by its tag's text `v`, which the plan's `ids` hold. Read
    # as anything else, the dropped day would be kept, or the held one unlinked and joined again, which gives it a new
    # relationship token, its stamp's second.
    days = LookupTarget(id="days", name="a target of days")
    first = LookupDate(id=date(2025, 1, 1), name="first day")
    second = LookupDate(id=date(2025, 1, 2), name="second day")
    await gom.save(LookupTargetDays(target=days, days=[first, second]), replace=["days"])
    loaded = await gom.load(LookupTargetDays, "days")
    assert loaded is not None and sorted(it.name or "" for it in loaded.days) == ["first day", "second day"]
    held = await gom.load(LookupDate, "2025-01-02")
    assert held is not None

    kept = LookupDate(id=date(2025, 1, 2), name="second day")
    await gom.save(LookupTargetDays(target=days, days=[kept]), replace=["days"])

    loaded = await gom.load(LookupTargetDays, "days")
    assert loaded is not None and [it.name for it in loaded.days] == ["second day"]
    again = await gom.load(LookupDate, "2025-01-02")
    assert again is not None and again.stamp == held.stamp
    dropped = await gom.load(LookupDate, "2025-01-01")
    assert dropped is not None and dropped.name == "first day"


async def test_a_dropped_target_of_a_date_id_nothing_refers_to_is_deleted_and_the_one_still_held_kept(
    gom: StatelessGraphObjectManager,
):
    # Replaced with DELETE_UNREFERENCED, the dropped target of a date id, which nothing else refers to, is deleted, and
    # the one still held is kept, unlinked from nothing, its stamp as it was: each told from the ids kept by their tags.
    days = LookupTarget(id="unreferenced days", name="a target of days")
    first = LookupDate(id=date(2025, 2, 1), name="dropped day")
    second = LookupDate(id=date(2025, 2, 2), name="held day")
    await gom.save(LookupTargetDays(target=days, days=[first, second]))
    held = await gom.load(LookupDate, "2025-02-02")
    assert held is not None

    kept = LookupDate(id=date(2025, 2, 2), name="held day")
    await gom.save(
        LookupTargetDays(target=days, days=[kept]),
        replace=["days"],
        removed_targets=RemovedTargets.DELETE_UNREFERENCED,
    )

    loaded = await gom.load(LookupTargetDays, "unreferenced days")
    assert loaded is not None and [it.name for it in loaded.days] == ["held day"]
    assert await gom.load(LookupDate, "2025-02-01") is None
    again = await gom.load(LookupDate, "2025-02-02")
    assert again is not None and again.stamp == held.stamp


async def test_a_target_of_a_uuid_id_still_held_is_kept_by_its_text_unquoted(gom: StatelessGraphObjectManager):
    # A UUID is held as its text, which the plan's `ids` key it by as a text, `t:` and the text: keyed by its JSON, the
    # held key would be unlinked and joined again, which gives it a new relationship token, its stamp's second.
    keys = LookupTarget(id="keys", name="a target of keys")
    first = LookupKey(id=uuid.UUID(int=1), name="first key")
    second = LookupKey(id=uuid.UUID(int=2), name="second key")
    await gom.save(LookupTargetKeys(target=keys, keys=[first, second]), replace=["keys"])
    held = await gom.load(LookupKey, str(uuid.UUID(int=2)))
    assert held is not None

    await gom.save(
        LookupTargetKeys(target=keys, keys=[LookupKey(id=uuid.UUID(int=2), name="second key")]), replace=["keys"]
    )

    loaded = await gom.load(LookupTargetKeys, "keys")
    assert loaded is not None and [it.name for it in loaded.keys] == ["second key"]
    again = await gom.load(LookupKey, uuid.UUID(int=2))
    assert again is not None and again.stamp == held.stamp


class LookupOwner(NodeFragment, labels=["SqlLookup", "LookupOwner"]):
    id: Annotated[str, NodeId()]
    name: str | None = None


class LookupKin(NodeFragment, labels=["SqlLookup", "LookupKin"]):
    """A node of a text id, beside nodes of its labels whose ids are a date or a number that reads as the same text."""

    id: Annotated[str, NodeId()]
    name: str | None = None


class LookupKinAny(NodeFragment, labels=["SqlLookup", "LookupKin"]):
    """LookupKin's labels with its id declared Any: it loads as the text of the tag it was read from."""

    id: Annotated[Any, NodeId()]
    name: str | None = None


class LookupOwnerKin(GraphView):
    owner: Annotated[LookupOwner, Root()]
    kin: Annotated[list[LookupKin], Relationship("OWNS")] = []


class LookupOwnerKinAny(GraphView):
    owner: Annotated[LookupOwner, Root()]
    kin: Annotated[list[LookupKinAny], Relationship("OWNS")] = []


async def cypher(pm: PersistenceManager, statement: str) -> list[Any]:
    return await pm.query(QuerySpecification[Any](cypher_statement(statement)))


async def owned_alike(pm: PersistenceManager) -> None:
    """The owner `o`, owning a date's node and the date's text's, and a number's node and the number's text's."""
    await cypher(
        pm,
        "CREATE (o:SqlLookup:LookupOwner {id: 'o', name: 'owner'}), "
        "(d:SqlLookup:LookupKin {id: date('2026-01-05'), name: 'date'}), "
        "(t:SqlLookup:LookupKin {id: '2026-01-05', name: 'text'}), (n:SqlLookup:LookupKin {id: 1, name: 'number'}), "
        "(s:SqlLookup:LookupKin {id: '1', name: 'one'}) "
        "CREATE (o)-[:OWNS]->(d), (o)-[:OWNS]->(t), (o)-[:OWNS]->(n), (o)-[:OWNS]->(s)",
    )


async def owned(pm: PersistenceManager) -> list[Any]:
    return await cypher(pm, "MATCH (:LookupOwner {id: 'o'})-[:OWNS]->(k) RETURN k.name ORDER BY k.name")


THE_TEXTS = [LookupKin(id="2026-01-05", name="text"), LookupKin(id="1", name="one")]


async def test_a_replace_that_keeps_a_text_ids_node_drops_the_relationship_to_a_date_or_a_number_whose_text_it_is(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    # A write plan keys a related node by its id, a text id apart from a date or a number that reads alike
    # (liberation-data/drivine-cypher-spec#391). Before, the plan keyed the text '2026-01-05' and the date alike, and
    # '1' and 1, so a replace kept all four, with no error. drivine4j's IdKindContract, ported.
    await owned_alike(pm)
    await gom.save(LookupOwnerKin(owner=LookupOwner(id="o", name="owner"), kin=THE_TEXTS), replace=["kin"])
    assert await owned(pm) == ["one", "text"]
    assert await cypher(pm, "MATCH (n:LookupKin) RETURN count(n)") == [4]


async def test_delete_unreferenced_deletes_the_dates_node_and_the_numbers_not_the_text_ids_nodes_the_view_holds(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    await owned_alike(pm)
    await gom.save(
        LookupOwnerKin(owner=LookupOwner(id="o", name="owner"), kin=THE_TEXTS),
        replace=["kin"],
        removed_targets=RemovedTargets.DELETE_UNREFERENCED,
    )
    assert await owned(pm) == ["one", "text"]
    assert await cypher(pm, "MATCH (n:LookupKin) RETURN n.name ORDER BY n.name") == ["one", "text"]


@pytest.mark.parametrize(
    "literal", ["date('2026-01-05')", "datetime('2026-01-05T01:02:03Z')", "localtime('10:11:12')", "duration('P1D')"]
)
async def test_a_replace_keeps_a_related_node_whose_id_declared_any_was_loaded_from_a_tag(
    gom: StatelessGraphObjectManager, pm: PersistenceManager, literal: str
):
    # Loaded as its tag's text, the id is held by a replace as the tag, as the plan keys the node: the plan marks a text
    # apart (liberation-data/drivine-cypher-spec#391), so keyed as the text it would be unlinked, and deleted under
    # DELETE_UNREFERENCED.
    await cypher(
        pm,
        "CREATE (o:SqlLookup:LookupOwner {id: 'o', name: 'owner'})"
        f"-[:OWNS]->(:SqlLookup:LookupKin {{id: {literal}}})",
    )
    loaded = await gom.load(LookupOwnerKinAny, "o")
    assert loaded is not None and len(loaded.kin) == 1
    await gom.save(loaded, replace=["kin"])
    assert await cypher(pm, "MATCH (:LookupOwner {id: 'o'})-[r:OWNS]->() RETURN count(r)") == [1]
    loaded = await gom.load(LookupOwnerKinAny, "o")
    assert loaded is not None
    await gom.save(loaded, replace=["kin"], removed_targets=RemovedTargets.DELETE_UNREFERENCED)
    assert await cypher(pm, "MATCH (:LookupOwner {id: 'o'})-[r:OWNS]->() RETURN count(r)") == [1]
    assert await cypher(pm, f"MATCH (k:LookupKin) WHERE k.id = {literal} RETURN count(k)") == [1]
    assert await cypher(pm, "MATCH (k:LookupKin) RETURN count(k)") == [1]


async def test_the_query_dsl_finds_a_uuid_id_by_the_text_a_save_holds_it_as(gom: StatelessGraphObjectManager):
    # The module's parameters take no UUID: the DSL binds one as the text a save holds it as (value_layout.prepared).
    await gom.save_all([LookupKey(id=uuid.UUID(int=it), name=f"key {it}") for it in (1, 2, 3)])
    k = refs(LookupKey)
    found = await gom.load_all(
        LookupKey,
        k.id.in_([uuid.UUID(int=2), uuid.UUID(int=3)]),
        order_by=k.id.asc(),
        seek=k.id.after(uuid.UUID(int=2)),
    )
    assert [it.name for it in found] == ["key 3"]


async def test_a_cascade_delete_by_a_date_ids_text_checks_its_depth_and_deletes_the_tree(
    gom: StatelessGraphObjectManager,
):
    # The root is found as a load finds it, both in the check that refuses a tree deeper than the view reads and in
    # the delete itself.
    isos = ["2026-02-01", "2026-02-02", "2026-02-03"]
    for i, iso in enumerate(isos):
        await gom.save(LookupDate(id=date.fromisoformat(iso), name=f"tree day {i}"))
    for i in range(1, len(isos)):
        assert await gom.edges.relate(node_ref(LookupDate, isos[i]), node_ref(LookupDate, isos[i - 1]), "DAY_PART_OF")
    with pytest.raises(DrivineError, match="1 node below that would be left behind"):
        await gom.delete(LookupDateTree, "2026-02-01", cascade=CascadeType.DELETE_ALL)
    assert len(await names(gom, LookupDate)) == 3

    assert await gom.delete(LookupDate, "2026-02-03") == 1
    assert await gom.delete(LookupDateTree, "2026-02-01", cascade=CascadeType.DELETE_ALL) == 2
    assert await names(gom, LookupDate) == []


async def test_a_patch_of_a_node_that_isnt_there_finds_none(gom: StatelessGraphObjectManager):
    patched = await gom.patch(LookupDate, "1999-01-01", set={"name": "none"})
    assert (patched.found, patched.applied, patched.changed) == (False, False, False)
    assert await names(gom, LookupDate) == []


async def test_a_str_id_finds_only_the_str_not_a_number_of_the_same_text(gom: StatelessGraphObjectManager):
    await gom.save(LookupTextNumber(id=6, name="number"))
    await gom.save(LookupText(id="6", name="string"))

    loaded = await gom.load(LookupText, "6")
    assert loaded is not None and loaded.name == "string"
    assert await gom.delete(LookupText, "6") == 1
    number = await gom.load(LookupTextNumber, 6)
    assert number is not None and number.name == "number"
    assert await gom.load(LookupText, "6") is None


async def test_text_that_is_no_number_finds_nothing_on_a_number_id_and_raises_nothing(
    gom: StatelessGraphObjectManager,
):
    await gom.save(LookupNumber(id=7, name="seven"))
    nowhere = node_ref(LookupNumber, "abc")

    assert await gom.load(LookupNumber, "abc") is None
    assert await gom.delete(LookupNumber, "abc") == 0
    assert await gom.edges.load_related(nowhere, "POINTS_TO", LookupTarget) == []
    assert not await gom.edges.relate(nowhere, TARGET, "POINTS_TO")
    patched = await gom.patch(LookupNumber, "abc", set={"name": "x"})
    assert (patched.found, patched.applied, patched.changed) == (False, False, False)
    seven = await gom.load(LookupNumber, "7")
    assert seven is not None and seven.name == "seven"


async def test_a_patch_by_a_null_id_finds_no_node_beside_one_whose_id_is_the_text_null(
    gom: StatelessGraphObjectManager,
):
    # #528: the plan keyed a null id as the text "null", and found the node whose id is that text.
    await gom.save(LookupText(id="null", name="the text null"))
    patched = await gom.patch(LookupText, None, set={"name": "patched"})
    assert (patched.found, patched.applied, patched.changed) == (False, False, False)
    loaded = await gom.load(LookupText, "null")
    assert loaded is not None and loaded.name == "the text null"


async def test_a_node_of_a_dollar_named_id_is_found_by_its_id(gom: StatelessGraphObjectManager):
    await gom.save(LookupPriced(code="p1", name="priced"))
    loaded = await gom.load(LookupPriced, "p1")
    assert loaded is not None and loaded.name == "priced"
    # A second save finds the node under the key the layout holds it as, and makes no second one.
    await gom.save(LookupPriced(code="p1", name="saved again"))
    assert await names(gom, LookupPriced) == ["saved again"]
    # So does a save_all, which writes a batch, and a relate, which finds both ends by their ids.
    await gom.save_all([LookupPriced(code="p1", name="batched")])
    assert await names(gom, LookupPriced) == ["batched"]
    assert await gom.edges.relate(node_ref(LookupPriced, "p1"), TARGET, "POINTS_TO")
    related = await gom.edges.load_related(node_ref(LookupPriced, "p1"), "POINTS_TO", LookupTarget)
    assert [it.id for it in related] == ["x"]
    patched = await gom.patch(LookupPriced, "p1", set={"name": "repriced"})
    assert patched.found
    loaded = await gom.load(LookupPriced, "p1")
    assert loaded is not None and loaded.name == "repriced"
    assert await gom.delete(LookupPriced, "p1") == 1
    assert await gom.load(LookupPriced, "p1") is None


# ----- Each lookup reads the index -----


async def assert_reads_index(
    gom: StatelessGraphObjectManager, pm: PersistenceManager, type_: type[Any], id_: Any, made: str
) -> None:
    """A load, a delete by id and load_related of a node of `type_` by `id_` each read the index named `made`."""
    sqlite = pm.type is DatabaseType.SQLITE
    await pm.execute(QuerySpecification[Any]("ANALYZE" if sqlite else "ANALYZE drivine_node"))
    registry = ModelRegistry()
    model = registry.fragment(type_)
    backend = gom.backend

    async def explain(text: str, bindings: dict[str, Any]) -> str:
        rows = await pm.query(
            QuerySpecification[Any](f"{'EXPLAIN QUERY PLAN' if sqlite else 'EXPLAIN'} {text}").bind(bindings)
        )
        return "\n".join(str(it["detail"] if sqlite else next(iter(it.values()))) for it in rows)

    def reads(row: str) -> re.Pattern[str]:
        """The plan's read of the index for the row `row`: on SQLite, the row each statement's FROM declares."""
        if sqlite:
            return re.compile(
                rf"SEARCH {row} USING (COVERING )?INDEX {index} \(<expr>=\? AND <expr>=\?\)", re.IGNORECASE
            )
        return re.compile(rf"Index (Only )?Scan using {index} on drivine_node", re.IGNORECASE)

    index = rf"drivine_{made}_\w+"
    bound = {"id": to_json(write(id_, "sqlite" if sqlite else "postgres"))}
    # A load and a delete read the node as the query DSL's row, `n_`, which SQLite's plan names.
    row = "n_"
    assert reads(row).search(await explain(backend.load_fragment(model).text, bound))
    assert reads(row).search(await explain(backend.delete(model).text, bound))
    related = backend.related(
        MatchedNode(model, id_, ()), "POINTS_TO", Direction.OUTGOING, registry.fragment(LookupTarget)
    )
    assert reads("a").search(await explain(related.text, dict(related.bindings)))


async def test_a_lookup_by_a_number_id_reads_the_uniqueness_constraints_index(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    """
    A load, a delete by id and load_related read the index the library makes over the id, a uniqueness constraint's
    ensured through `gom.constraints`, which the shared module's schema plan makes as layout 2 makes it: over the
    property's JSON on Postgres, and its class and key on SQLite, which the module's hasId compares. On Postgres and
    SQLite, whose planners are read here; Oracle's lookups are planned in drivine4j's suite.
    """
    if pm.type is DatabaseType.ORACLE:
        pytest.skip("Oracle's lookups by id are planned in drivine4j's suite.")
    constraint = uniqueness_constraint("LookupNumber", "id")
    await gom.save_all([LookupNumber(id=1000 + i, name="filler") for i in range(3000)])
    await gom.constraints.ensure(constraint)
    try:
        await assert_reads_index(gom, pm, LookupNumber, 1500, "uq2_LookupNumberid")
    finally:
        await gom.constraints.drop(constraint)


async def test_a_lookup_by_a_date_id_reads_the_range_index_where_it_is_made_over_the_ids_class_and_key(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    """
    On SQLite, a range index ensured through `gom.indexes` over a date id, made over the value's class and key as the
    module's hasId compares them, is read by a load, a delete by id and load_related. Postgres's range index is over
    the property's text, which a lookup by the id's JSON doesn't read: its uniqueness constraint's index is.
    """
    if pm.type is not DatabaseType.SQLITE:
        pytest.skip("Postgres's range index is over the property's text, not its JSON.")
    index = range_index("LookupDate", "id")
    await gom.save_all([LookupDate(id=date(2000, 1, 1) + timedelta(days=i), name="filler") for i in range(3000)])
    await gom.indexes.ensure(index)
    try:
        await assert_reads_index(gom, pm, LookupDate, date(2001, 1, 1), "ix2_LookupDateid")
    finally:
        await gom.indexes.drop(index)


async def test_each_lookup_by_id_reads_the_index_by_the_row_its_statements_from_declares(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    """
    A view's load and its deletes, a cascade's included, read the node as the query DSL's row, `n_`; a write's reads of
    the node it saves, held_properties and found_stamp, read it as `n`, as the write itself does. Each reads the index.
    """
    if pm.type is not DatabaseType.SQLITE:
        pytest.skip("SQLite's rows: Postgres's and Oracle's read the node as n.")
    index = range_index("LookupDate", "id")
    await gom.save_all([LookupDate(id=date(2000, 1, 1) + timedelta(days=i), name="filler") for i in range(3000)])
    await gom.indexes.ensure(index)
    try:
        await pm.execute(QuerySpecification[Any]("ANALYZE"))
        registry = ModelRegistry()
        model, view = registry.fragment(LookupDate), registry.view(LookupDateTree)
        backend = gom.backend
        bound = {"id": to_json(write(date(2001, 1, 1), "sqlite"))}

        async def plan(text: str) -> str:
            rows = await pm.query(QuerySpecification[Any](f"EXPLAIN QUERY PLAN {text}").bind(bound))
            return "\n".join(str(it["detail"]) for it in rows)

        def reads(row: str) -> re.Pattern[str]:
            return re.compile(
                rf"SEARCH {row} USING (COVERING )?INDEX drivine_ix2_LookupDateid_\w+ \(<expr>=\? AND <expr>=\?\)"
            )

        for statement in (
            backend.load_view(view),
            backend.delete(view),
            backend.delete(view, None, CascadeType.DELETE_ALL),
            backend.delete(view, None, CascadeType.DELETE_ORPHAN),
        ):
            assert reads("n_").search(await plan(statement.text))
        assert reads("n").search(await plan(backend.held_properties(model).text))
        assert reads("n").search(await plan(backend.found_stamp(model).text))
    finally:
        await gom.indexes.drop(index)


async def test_a_lookup_by_a_str_id_reads_the_uniqueness_constraints_index(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    """As a number id's: a load, a delete by id and load_related of a str id read its uniqueness constraint's index."""
    if pm.type is DatabaseType.ORACLE:
        pytest.skip("Oracle's lookups by id are planned in drivine4j's suite.")
    constraint = uniqueness_constraint("LookupText", "id")
    await gom.save_all([LookupText(id=f"t{1000 + i}", name="filler") for i in range(3000)])
    await gom.constraints.ensure(constraint)
    try:
        await assert_reads_index(gom, pm, LookupText, "t1500", "uq2_LookupTextid")
    finally:
        await gom.constraints.drop(constraint)


async def test_a_saves_lookup_on_postgres_reads_the_uniqueness_constraints_index(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    """
    A save finds each node through the lookup the module's write function prepares on its connection, `drivine_find`,
    by the id's JSON: for a number, a date and a str id it reads the id's uniqueness constraint's index. Called in one
    transaction, so on one connection, the statement it prepared there is explained for the id's JSON.
    """
    if pm.type is not DatabaseType.POSTGRES:
        pytest.skip("drivine_find is Postgres's write function's lookup.")
    cases: list[tuple[type[Any], str, list[Any], Any]] = [
        (LookupNumber, "LookupNumber", [LookupNumber(id=1000 + i) for i in range(3000)], 1500),
        (
            LookupDate,
            "LookupDate",
            [LookupDate(id=date(2000, 1, 1) + timedelta(days=i)) for i in range(3000)],
            date(2001, 1, 1),
        ),
        (LookupText, "LookupText", [LookupText(id=f"t{1000 + i}") for i in range(3000)], "t1500"),
    ]
    for type_, label, fillers, id_ in cases:
        constraint = uniqueness_constraint(label, "id")
        await gom.save_all(fillers)
        await gom.constraints.ensure(constraint)
        try:
            await pm.execute(QuerySpecification[Any]("ANALYZE drivine_node"))
            labels = sql_text(to_json(ModelRegistry().fragment(type_).labels))
            id_json = sql_text(to_json(write(id_, "postgres")))
            async with transaction(rollback=True):
                await pm.query(
                    QuerySpecification[Any](
                        f"SELECT (drivine_find(CAST({labels} AS jsonb), 'id', CAST({id_json} AS jsonb))).nid"
                    )
                )
                like = sql_text(f'%"{label}"%')
                prepared = await pm.query(
                    QuerySpecification[Any](
                        "SELECT name FROM pg_prepared_statements "
                        f"WHERE name LIKE 'drivine_find_%' AND statement LIKE {like}"
                    )
                )
                assert len(prepared) == 1, prepared
                rows = await pm.query(
                    QuerySpecification[Any](f"EXPLAIN EXECUTE {prepared[0]['name']}(CAST({id_json} AS jsonb))")
                )
            plan = "\n".join(str(next(iter(it.values()))) for it in rows)
            assert re.search(
                rf"Index (Only )?Scan using drivine_uq2_{label}id_\w+ on drivine_node[\s\S]*?"
                r"Index Cond: \(\(props -> 'id'::text\) = ",
                plan,
                re.IGNORECASE,
            ), f"the save's lookup of a {label}:\n{plan}"
            assert not re.search(r"Seq Scan on drivine_node\b", plan), f"the save's lookup of a {label}:\n{plan}"
        finally:
            await gom.constraints.drop(constraint)


async def test_a_ref_or_a_patch_step_without_the_ids_json_is_refused_by_name_on_postgres(pm: PersistenceManager):
    """
    A plan whose `ref` or `patch` step carries only the id's text, as plans before layout 2 did, is refused by name by
    the store's write function, rather than finding no node and reading as absent
    (liberation-data/drivine-cypher-spec#245).
    """
    if pm.type is not DatabaseType.POSTGRES:
        pytest.skip("drivine_write is Postgres's write function.")
    shapes = sql_text('[{"labels": ["LookupText"], "key": "id"}]')
    for step in (
        '{"op": "ref", "r": 0, "s": 0, "k": "t1"}',
        '{"op": "patch", "s": 0, "k": "t1", "i": "0", "fresh": "f"}',
    ):
        plan = sql_text(f"[{step}]")
        statement = f"SELECT drivine_write(CAST({plan} AS jsonb), CAST({shapes} AS jsonb), 'mark', 'COUNT', NULL)"
        with pytest.raises(DrivineError) as refused:
            await pm.query(QuerySpecification[Any](statement))
        refusal = messages(refused.value)
        assert "A plan step that finds a node by id carries no id" in refusal, (step, refusal)


class LookupDay(NodeFragment, labels=["SqlLookup", "LookupDay"]):
    id: Annotated[date, NodeId()]
    name: str | None = None


class LookupDayAt(NodeFragment, labels=["SqlLookup", "LookupDay"]):
    id: Annotated[datetime, NodeId()]
    name: str | None = None


class LookupDayAsText(NodeFragment, labels=["SqlLookup", "LookupDay"]):
    """A day's labels with its id declared text: what a model of a graph another writer made may declare."""

    id: Annotated[str, NodeId()]
    name: str | None = None


class LookupDayAsStampedText(NodeFragment, labels=["SqlLookup", "LookupDay"]):
    id: Annotated[str, NodeId()]
    name: str | None = None
    stamp: Annotated[str | None, NodeStamp()] = None


class LookupTargetTextDays(GraphView):
    """The target joined to days whose id it declares text."""

    target: Annotated[LookupTarget, Root()]
    days: Annotated[list[LookupDayAsText], Relationship("ON_TEXT_DAY")] = []


REFUSED = re.escape(
    "LookupDayAsText's NodeId id is declared a str, but the node whose id is '2026-01-05' holds it as a date:"
    " declare id as a date to load it."
)


async def test_a_load_of_a_date_id_declared_text_is_refused_by_name(gom: StatelessGraphObjectManager):
    # The id would load as its text, which a lookup never finds: a save of it would make a second node (#560).
    await gom.save(LookupDay(id=date(2026, 1, 5), name="day"))
    with pytest.raises(DrivineError, match=REFUSED):
        await gom.load_all(LookupDayAsText)
    assert [it.id for it in await gom.load_all(LookupDay)] == [date(2026, 1, 5)]


async def test_a_load_of_a_date_id_declared_text_with_a_stamp_is_refused_by_name(gom: StatelessGraphObjectManager):
    # Before, its save was refused as stale, as though the node had been deleted.
    await gom.save(LookupDay(id=date(2026, 1, 5), name="day"))
    with pytest.raises(DrivineError, match=r"LookupDayAsStampedText's NodeId id is declared a str, .* as a date"):
        await gom.load_all(LookupDayAsStampedText)


async def test_a_datetime_id_declared_text_is_refused_with_its_kind(gom: StatelessGraphObjectManager):
    await gom.save(LookupDayAt(id=datetime(2026, 1, 5, 10, tzinfo=UTC), name="at ten"))
    with pytest.raises(DrivineError, match=r"holds it as a datetime: declare id as a datetime to load it\."):
        await gom.load_all(LookupDayAsText)


async def test_a_related_node_whose_date_id_is_declared_text_is_refused_by_name(gom: StatelessGraphObjectManager):
    await gom.save(LookupDay(id=date(2026, 1, 5), name="day"))
    assert await gom.edges.relate(TARGET, node_ref(LookupDay, date(2026, 1, 5)), "ON_TEXT_DAY")
    with pytest.raises(DrivineError, match=REFUSED):
        await gom.load_all(LookupTargetTextDays)


class LookupDayAsAny(NodeFragment, labels=["SqlLookup", "LookupDay"]):
    """A day's labels with its id and a property declared Any: each loads as the text of the tag it was read from."""

    id: Annotated[Any, NodeId()]
    name: str | None = None
    when: Any = None


async def test_a_date_id_declared_any_saved_back_is_one_node_that_keeps_its_date(gom: StatelessGraphObjectManager):
    # Its id loads as its text, which the save looks up and writes as the date it was read from (#561).
    await gom.save(LookupDay(id=date(2026, 1, 5), name="day"))
    [loaded] = await gom.load_all(LookupDayAsAny)
    await gom.save(loaded.model_copy(update={"name": "again"}))
    assert [(it.id, it.name) for it in await gom.load_all(LookupDay)] == [(date(2026, 1, 5), "again")]


async def test_an_id_declared_any_given_a_new_text_is_a_node_of_that_text(gom: StatelessGraphObjectManager):
    await gom.save(LookupDay(id=date(2026, 1, 5), name="day"))
    [loaded] = await gom.load_all(LookupDayAsAny)
    await gom.save(loaded.model_copy(update={"id": "2026-01-05", "name": "text"}))
    assert sorted(it.name or "" for it in await gom.load_all(LookupDayAsAny)) == ["day", "text"]


async def test_a_temporal_value_a_query_gave_bound_back_finds_the_node_it_was_read_from(pm: PersistenceManager):
    # A row's temporal value reads as its text, and binds back as the value it was read from, not as text (#561).
    await pm.execute(
        QuerySpecification[Any](cypher_statement("CREATE (:SqlLookup:LookupDay {id: date('2026-01-05')})"))
    )
    [held] = await pm.query(QuerySpecification[Any](cypher_statement("MATCH (n:LookupDay) RETURN n.id")))
    assert held == "2026-01-05"
    found = await pm.query(
        QuerySpecification[Any](cypher_statement("MATCH (n:LookupDay) WHERE n.id = $id RETURN count(n)")).bind(
            {"id": held}
        )
    )
    assert found == [1]


class LookupDayWhen(NodeFragment, labels=["SqlLookup", "LookupDay"]):
    id: Annotated[date, NodeId()]
    name: str | None = None
    when: date | None = None


async def test_a_property_declared_any_saved_back_keeps_the_date_a_where_finds_it_by(gom: StatelessGraphObjectManager):
    await gom.save(LookupDayWhen(id=date(2026, 1, 5), name="day", when=date(2026, 2, 1)))
    [loaded] = await gom.load_all(LookupDayAsAny)
    await gom.save(loaded.model_copy(update={"name": "again"}))
    found = await gom.load_all(LookupDayWhen, where=refs(LookupDayWhen).when.eq(date(2026, 2, 1)))
    assert [(it.name, it.when) for it in found] == [("again", date(2026, 2, 1))]


async def test_a_date_id_over_a_moment_is_refused_by_name_as_one_a_save_couldnt_find(
    gom: StatelessGraphObjectManager,
):
    await gom.save(LookupDayAt(id=datetime(2026, 1, 5, tzinfo=UTC), name="midnight"))
    with pytest.raises(
        DrivineError,
        match=r"LookupDay's NodeId id is declared a date, .* holds it as a datetime, which"
        r" a date can't save back: declare id as a datetime to load it\.",
    ):
        await gom.load_all(LookupDay)


class LookupDaySpan(NodeFragment, labels=["SqlLookup", "LookupDay"]):
    id: Annotated[timedelta, NodeId()]
    name: str | None = None


@pytest.mark.parametrize(
    ("made", "type_"),
    [
        ("datetime('2026-01-05T10:00:00.5+02:00')", LookupDayAt),
        ("datetime('2026-01-05T10:00:00Z')", LookupDayAt),
        ("localdatetime('2026-01-05T10:00:00.123456')", LookupDayAt),
        ("duration('P1DT2H30M0.5S')", LookupDaySpan),
    ],
)
async def test_a_temporal_id_at_an_offset_or_a_fraction_saved_back_is_one_node(
    pm: PersistenceManager, gom: StatelessGraphObjectManager, made: str, type_: type[Any]
):
    # Another writer's value loads as the Python value, which a save writes back as the value the store holds (#561).
    await pm.execute(
        QuerySpecification[Any](cypher_statement(f"CREATE (:SqlLookup:LookupDay {{id: {made}, name: 'a'}})"))
    )
    [loaded] = await gom.load_all(type_)
    await gom.save(loaded.model_copy(update={"name": "again"}))
    assert await names(gom, type_) == ["again"]
