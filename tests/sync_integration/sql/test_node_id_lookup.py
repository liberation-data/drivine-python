# Written by scripts/unasync.py from tests/integration/sql/test_node_id_lookup.py:
# change that, and run the script again.
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
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any

import pytest

from drivine.backend.matched_node import MatchedNode
from drivine.backend.sql.sql_json import to_json
from drivine.backend.sql.value_layout import write
from drivine.model.model_registry import ModelRegistry
from drivine.sync import (
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
    node_ref,
    range_index,
    refs,
    uniqueness_constraint,
)
from tests.sync_integration.sql.sql_seed import SqlSeed


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
def cleared(gom: StatelessGraphObjectManager, pm: PersistenceManager) -> Iterator[None]:
    seed = SqlSeed(pm, "SqlLookup")
    seed.clear()
    gom.save(LookupTarget(id="x", name="the target"))
    yield
    seed.clear()


def names(gom: StatelessGraphObjectManager, type_: type[Any]) -> list[str]:
    """The names of the nodes of `type_`'s labels, in order."""
    return sorted(it.name for it in gom.load_all(type_))


# ----- A temporal id -----


def test_a_date_id_saved_twice_is_one_node_loaded_by_the_ids_text(gom: StatelessGraphObjectManager):
    gom.save(LookupDate(id=date(2024, 1, 2), name="first"))
    gom.save(LookupDate(id=date(2024, 1, 2), name="second"))
    assert names(gom, LookupDate) == ["second"]
    loaded = gom.load(LookupDate, "2024-01-02")
    assert loaded is not None and loaded.name == "second"


def test_a_date_id_saved_twice_in_one_save_all_is_one_node(gom: StatelessGraphObjectManager):
    gom.save_all(
        [LookupDate(id=date(2024, 1, 3), name="batch first"), LookupDate(id=date(2024, 1, 3), name="batch second")]
    )
    assert names(gom, LookupDate) == ["batch second"]
    loaded = gom.load(LookupDate, date(2024, 1, 3))
    assert loaded is not None and loaded.id == date(2024, 1, 3)


# ----- Load, delete by id and load_related, for a number, a date and a str id -----


def lookups(gom: StatelessGraphObjectManager, type_: type[Any], node: Any, text: str) -> None:
    """Saves `node`, joins it to the target, and finds it by `text` through a load, load_related and a delete."""
    gom.save(node)
    assert gom.edges.relate(node_ref(type_, text), TARGET, "POINTS_TO")
    loaded = gom.load(type_, text)
    assert loaded is not None and loaded.name == node.name
    related = gom.edges.load_related(node_ref(type_, text), "POINTS_TO", LookupTarget)
    assert [it.id for it in related] == ["x"]
    assert gom.delete(type_, text) == 1
    assert gom.load(type_, text) is None


def test_a_number_id_is_found_by_its_text(gom: StatelessGraphObjectManager):
    lookups(gom, LookupNumber, LookupNumber(id=5, name="five"), "5")


def test_a_date_id_is_found_by_its_text(gom: StatelessGraphObjectManager):
    lookups(gom, LookupDate, LookupDate(id=date(2024, 3, 4), name="march"), "2024-03-04")


def test_a_str_id_is_found_by_its_text(gom: StatelessGraphObjectManager):
    lookups(gom, LookupText, LookupText(id="t5", name="text"), "t5")


# ----- Relate (a `ref` step) and patch (a `patch` step), each finding its node by the id's JSON -----


def refs_and_patches(gom: StatelessGraphObjectManager, type_: type[Any], node: Any, text: str, absent: str):
    """
    Joins `node`, saved, to the target by refs given the id as text and as its value; patches its name; and refers to
    a node of the class that isn't there, `absent`, which is not made, and joins nothing.
    """
    gom.save(node)
    id_ = node.id
    assert gom.edges.relate(node_ref(type_, text), TARGET, "REFERS_TO")
    assert gom.edges.relate(node_ref(type_, id_), TARGET, "REFERS_TO", {}, RelateMode.CREATE)
    assert gom.edges.unrelate(node_ref(type_, text), TARGET, "REFERS_TO") == 2
    assert gom.edges.relate(node_ref(type_, id_), TARGET, "REFERS_TO")

    patched = gom.patch(type_, text, set={"name": "renamed"})
    assert (patched.found, patched.applied, patched.changed) == (True, True, True)
    loaded = gom.load(type_, text)
    assert loaded is not None and loaded.name == "renamed"
    [all_] = gom.patch_all([type_(id=id_, name="patched all")], only=["name"])
    assert (all_.found, all_.applied, all_.changed) == (True, True, True)
    loaded = gom.load(type_, id_)
    assert loaded is not None and loaded.name == "patched all"

    assert not gom.edges.relate(node_ref(type_, absent), TARGET, "REFERS_TO")
    assert not gom.edges.relate(TARGET, node_ref(type_, absent), "REFERS_TO")
    assert gom.edges.load_related(TARGET, "REFERS_TO", type_) == []
    assert gom.edges.unrelate_all(node_ref(type_, text), "REFERS_TO") == 1


def test_a_node_of_a_number_id_is_related_and_patched(gom: StatelessGraphObjectManager):
    refs_and_patches(gom, LookupNumber, LookupNumber(id=21, name="a"), "21", "22")


def test_a_node_of_a_date_id_is_related_and_patched(gom: StatelessGraphObjectManager):
    refs_and_patches(gom, LookupDate, LookupDate(id=date(2024, 5, 6), name="a"), "2024-05-06", "2024-05-07")


def test_a_node_of_a_str_id_is_related_and_patched(gom: StatelessGraphObjectManager):
    refs_and_patches(gom, LookupText, LookupText(id="t21", name="a"), "t21", "t22")


def test_a_node_of_a_float_id_is_related_and_patched(gom: StatelessGraphObjectManager):
    # Oracle holds a float as a binary double: each write block finds it by the id bound as one, read EXTENDED (#754).
    refs_and_patches(gom, LookupFloat, LookupFloat(id=2.5, name="a"), "2.5", "3.5")


def test_a_node_of_a_dollar_named_id_is_related_unrelated_patched_all_and_unrelated_all(
    gom: StatelessGraphObjectManager,
):
    gom.save(LookupPriced(code="p2", name="a"))
    priced = node_ref(LookupPriced, "p2")
    assert gom.edges.relate(priced, TARGET, "REFERS_TO", {}, RelateMode.CREATE)
    assert gom.edges.relate(TARGET, priced, "REFERS_TO")
    assert gom.edges.unrelate(priced, TARGET, "REFERS_TO") == 1
    [all_] = gom.patch_all([LookupPriced(code="p2", name="patched all")], only=["name"])
    assert (all_.found, all_.applied, all_.changed) == (True, True, True)
    loaded = gom.load(LookupPriced, "p2")
    assert loaded is not None and loaded.name == "patched all"
    assert gom.edges.unrelate_all(priced, "REFERS_TO", Direction.INCOMING) == 1
    assert gom.edges.load_related(TARGET, "REFERS_TO", LookupPriced) == []


def test_each_node_of_a_save_all_is_found_by_a_float_a_date_and_a_dollar_named_id_and_handed_its_stamp(
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

    def loaded() -> list[str | None]:
        found = [
            gom.load(LookupFloat, 0.1),
            gom.load(LookupDate, "2024-07-08"),
            gom.load(LookupPriced, "p3"),
        ]
        return [it.stamp if it is not None else None for it in found]

    made = batch("")
    gom.save_all(made)
    first = loaded()
    assert all(it is not None and ":" in it for it in first)
    assert [it.stamp for it in made] == first

    again = batch(" again")
    gom.save_all(again)
    assert names(gom, LookupFloat) == ["float again"]
    assert names(gom, LookupDate) == ["date again"]
    assert names(gom, LookupPriced) == ["priced again"]
    assert [it.stamp for it in again] == [f"{(it or '').split(':')[0]}:" for it in loaded()]


def test_a_views_related_nodes_of_a_date_id_are_handed_their_stamps(gom: StatelessGraphObjectManager):
    days = LookupTarget(id="stamped days", name="a target of stamped days")
    first = LookupDate(id=date(2025, 3, 1), name="first day")
    second = LookupDate(id=date(2025, 3, 2), name="second day")
    gom.save(LookupTargetDays(target=days, days=[first, second]))
    for it, iso in ((first, "2025-03-01"), (second, "2025-03-02")):
        loaded = gom.load(LookupDate, iso)
        assert loaded is not None and loaded.stamp is not None
        assert it.stamp == loaded.stamp


def test_a_target_of_a_date_id_is_unlinked_by_its_tags_text_and_the_one_still_held_kept(
    gom: StatelessGraphObjectManager,
):
    # A save that replaces a list keeps the targets the object still holds, told by their ids as the plan keys them,
    # and unlinks the rest: a target of a date id is read back by its tag's text `v`, which the plan's `ids` hold. Read
    # as anything else, the dropped day would be kept, or the held one unlinked and joined again, which gives it a new
    # relationship token, its stamp's second.
    days = LookupTarget(id="days", name="a target of days")
    first = LookupDate(id=date(2025, 1, 1), name="first day")
    second = LookupDate(id=date(2025, 1, 2), name="second day")
    gom.save(LookupTargetDays(target=days, days=[first, second]), replace=["days"])
    loaded = gom.load(LookupTargetDays, "days")
    assert loaded is not None and sorted(it.name or "" for it in loaded.days) == ["first day", "second day"]
    held = gom.load(LookupDate, "2025-01-02")
    assert held is not None

    kept = LookupDate(id=date(2025, 1, 2), name="second day")
    gom.save(LookupTargetDays(target=days, days=[kept]), replace=["days"])

    loaded = gom.load(LookupTargetDays, "days")
    assert loaded is not None and [it.name for it in loaded.days] == ["second day"]
    again = gom.load(LookupDate, "2025-01-02")
    assert again is not None and again.stamp == held.stamp
    dropped = gom.load(LookupDate, "2025-01-01")
    assert dropped is not None and dropped.name == "first day"


def test_a_dropped_target_of_a_date_id_nothing_refers_to_is_deleted_and_the_one_still_held_kept(
    gom: StatelessGraphObjectManager,
):
    # Replaced with DELETE_UNREFERENCED, the dropped target of a date id, which nothing else refers to, is deleted, and
    # the one still held is kept, unlinked from nothing, its stamp as it was: each told from the ids kept by their tags.
    days = LookupTarget(id="unreferenced days", name="a target of days")
    first = LookupDate(id=date(2025, 2, 1), name="dropped day")
    second = LookupDate(id=date(2025, 2, 2), name="held day")
    gom.save(LookupTargetDays(target=days, days=[first, second]))
    held = gom.load(LookupDate, "2025-02-02")
    assert held is not None

    kept = LookupDate(id=date(2025, 2, 2), name="held day")
    gom.save(
        LookupTargetDays(target=days, days=[kept]),
        replace=["days"],
        removed_targets=RemovedTargets.DELETE_UNREFERENCED,
    )

    loaded = gom.load(LookupTargetDays, "unreferenced days")
    assert loaded is not None and [it.name for it in loaded.days] == ["held day"]
    assert gom.load(LookupDate, "2025-02-01") is None
    again = gom.load(LookupDate, "2025-02-02")
    assert again is not None and again.stamp == held.stamp


def test_a_target_of_a_uuid_id_still_held_is_kept_by_its_text_unquoted(gom: StatelessGraphObjectManager):
    # A UUID is held as its text, which the plan's `ids` key it by, unquoted: keyed by its JSON, the held key would be
    # unlinked and joined again, which gives it a new relationship token, its stamp's second.
    keys = LookupTarget(id="keys", name="a target of keys")
    first = LookupKey(id=uuid.UUID(int=1), name="first key")
    second = LookupKey(id=uuid.UUID(int=2), name="second key")
    gom.save(LookupTargetKeys(target=keys, keys=[first, second]), replace=["keys"])
    held = gom.load(LookupKey, str(uuid.UUID(int=2)))
    assert held is not None

    gom.save(LookupTargetKeys(target=keys, keys=[LookupKey(id=uuid.UUID(int=2), name="second key")]), replace=["keys"])

    loaded = gom.load(LookupTargetKeys, "keys")
    assert loaded is not None and [it.name for it in loaded.keys] == ["second key"]
    again = gom.load(LookupKey, uuid.UUID(int=2))
    assert again is not None and again.stamp == held.stamp


def test_the_query_dsl_finds_a_uuid_id_by_the_text_a_save_holds_it_as(gom: StatelessGraphObjectManager):
    # The module's parameters take no UUID: the DSL binds one as the text a save holds it as (value_layout.prepared).
    gom.save_all([LookupKey(id=uuid.UUID(int=it), name=f"key {it}") for it in (1, 2, 3)])
    k = refs(LookupKey)
    found = gom.load_all(
        LookupKey,
        k.id.in_([uuid.UUID(int=2), uuid.UUID(int=3)]),
        order_by=k.id.asc(),
        seek=k.id.after(uuid.UUID(int=2)),
    )
    assert [it.name for it in found] == ["key 3"]


def test_a_cascade_delete_by_a_date_ids_text_checks_its_depth_and_deletes_the_tree(
    gom: StatelessGraphObjectManager,
):
    # The root is found as a load finds it, both in the check that refuses a tree deeper than the view reads and in
    # the delete itself.
    isos = ["2026-02-01", "2026-02-02", "2026-02-03"]
    for i, iso in enumerate(isos):
        gom.save(LookupDate(id=date.fromisoformat(iso), name=f"tree day {i}"))
    for i in range(1, len(isos)):
        assert gom.edges.relate(node_ref(LookupDate, isos[i]), node_ref(LookupDate, isos[i - 1]), "DAY_PART_OF")
    with pytest.raises(DrivineError, match="1 node below that would be left behind"):
        gom.delete(LookupDateTree, "2026-02-01", cascade=CascadeType.DELETE_ALL)
    assert len(names(gom, LookupDate)) == 3

    assert gom.delete(LookupDate, "2026-02-03") == 1
    assert gom.delete(LookupDateTree, "2026-02-01", cascade=CascadeType.DELETE_ALL) == 2
    assert names(gom, LookupDate) == []


def test_a_patch_of_a_node_that_isnt_there_finds_none(gom: StatelessGraphObjectManager):
    patched = gom.patch(LookupDate, "1999-01-01", set={"name": "none"})
    assert (patched.found, patched.applied, patched.changed) == (False, False, False)
    assert names(gom, LookupDate) == []


def test_a_str_id_finds_only_the_str_not_a_number_of_the_same_text(gom: StatelessGraphObjectManager):
    gom.save(LookupTextNumber(id=6, name="number"))
    gom.save(LookupText(id="6", name="string"))

    loaded = gom.load(LookupText, "6")
    assert loaded is not None and loaded.name == "string"
    assert gom.delete(LookupText, "6") == 1
    number = gom.load(LookupTextNumber, 6)
    assert number is not None and number.name == "number"
    assert gom.load(LookupText, "6") is None


def test_text_that_is_no_number_finds_nothing_on_a_number_id_and_raises_nothing(
    gom: StatelessGraphObjectManager,
):
    gom.save(LookupNumber(id=7, name="seven"))
    nowhere = node_ref(LookupNumber, "abc")

    assert gom.load(LookupNumber, "abc") is None
    assert gom.delete(LookupNumber, "abc") == 0
    assert gom.edges.load_related(nowhere, "POINTS_TO", LookupTarget) == []
    assert not gom.edges.relate(nowhere, TARGET, "POINTS_TO")
    patched = gom.patch(LookupNumber, "abc", set={"name": "x"})
    assert (patched.found, patched.applied, patched.changed) == (False, False, False)
    seven = gom.load(LookupNumber, "7")
    assert seven is not None and seven.name == "seven"


def test_a_patch_by_a_null_id_finds_no_node_beside_one_whose_id_is_the_text_null(
    gom: StatelessGraphObjectManager,
):
    # #528: the plan keyed a null id as the text "null", and found the node whose id is that text.
    gom.save(LookupText(id="null", name="the text null"))
    patched = gom.patch(LookupText, None, set={"name": "patched"})
    assert (patched.found, patched.applied, patched.changed) == (False, False, False)
    loaded = gom.load(LookupText, "null")
    assert loaded is not None and loaded.name == "the text null"


def test_a_node_of_a_dollar_named_id_is_found_by_its_id(gom: StatelessGraphObjectManager):
    gom.save(LookupPriced(code="p1", name="priced"))
    loaded = gom.load(LookupPriced, "p1")
    assert loaded is not None and loaded.name == "priced"
    # A second save finds the node under the key the layout holds it as, and makes no second one.
    gom.save(LookupPriced(code="p1", name="saved again"))
    assert names(gom, LookupPriced) == ["saved again"]
    # So does a save_all, which writes a batch, and a relate, which finds both ends by their ids.
    gom.save_all([LookupPriced(code="p1", name="batched")])
    assert names(gom, LookupPriced) == ["batched"]
    assert gom.edges.relate(node_ref(LookupPriced, "p1"), TARGET, "POINTS_TO")
    related = gom.edges.load_related(node_ref(LookupPriced, "p1"), "POINTS_TO", LookupTarget)
    assert [it.id for it in related] == ["x"]
    patched = gom.patch(LookupPriced, "p1", set={"name": "repriced"})
    assert patched.found
    loaded = gom.load(LookupPriced, "p1")
    assert loaded is not None and loaded.name == "repriced"
    assert gom.delete(LookupPriced, "p1") == 1
    assert gom.load(LookupPriced, "p1") is None


# ----- Each lookup reads the index -----


def assert_reads_index(
    gom: StatelessGraphObjectManager, pm: PersistenceManager, type_: type[Any], id_: Any, made: str
) -> None:
    """A load, a delete by id and load_related of a node of `type_` by `id_` each read the index named `made`."""
    sqlite = pm.type is DatabaseType.SQLITE
    pm.execute(QuerySpecification[Any]("ANALYZE" if sqlite else "ANALYZE drivine_node"))
    registry = ModelRegistry()
    model = registry.fragment(type_)
    backend = gom.backend

    def explain(text: str, bindings: dict[str, Any]) -> str:
        rows = pm.query(
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
    assert reads(row).search(explain(backend.load_fragment(model).text, bound))
    assert reads(row).search(explain(backend.delete(model).text, bound))
    related = backend.related(
        MatchedNode(model, id_, ()), "POINTS_TO", Direction.OUTGOING, registry.fragment(LookupTarget)
    )
    assert reads("a").search(explain(related.text, dict(related.bindings)))


def test_a_lookup_by_a_number_id_reads_the_uniqueness_constraints_index(
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
    gom.save_all([LookupNumber(id=1000 + i, name="filler") for i in range(3000)])
    gom.constraints.ensure(constraint)
    try:
        assert_reads_index(gom, pm, LookupNumber, 1500, "uq2_LookupNumberid")
    finally:
        gom.constraints.drop(constraint)


def test_a_lookup_by_a_date_id_reads_the_range_index_where_it_is_made_over_the_ids_class_and_key(
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
    gom.save_all([LookupDate(id=date(2000, 1, 1) + timedelta(days=i), name="filler") for i in range(3000)])
    gom.indexes.ensure(index)
    try:
        assert_reads_index(gom, pm, LookupDate, date(2001, 1, 1), "ix2_LookupDateid")
    finally:
        gom.indexes.drop(index)


def test_each_lookup_by_id_reads_the_index_by_the_row_its_statements_from_declares(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    """
    A view's load and its deletes, a cascade's included, read the node as the query DSL's row, `n_`; a write's reads of
    the node it saves, held_properties and found_stamp, read it as `n`, as the write itself does. Each reads the index.
    """
    if pm.type is not DatabaseType.SQLITE:
        pytest.skip("SQLite's rows: Postgres's and Oracle's read the node as n.")
    index = range_index("LookupDate", "id")
    gom.save_all([LookupDate(id=date(2000, 1, 1) + timedelta(days=i), name="filler") for i in range(3000)])
    gom.indexes.ensure(index)
    try:
        pm.execute(QuerySpecification[Any]("ANALYZE"))
        registry = ModelRegistry()
        model, view = registry.fragment(LookupDate), registry.view(LookupDateTree)
        backend = gom.backend
        bound = {"id": to_json(write(date(2001, 1, 1), "sqlite"))}

        def plan(text: str) -> str:
            rows = pm.query(QuerySpecification[Any](f"EXPLAIN QUERY PLAN {text}").bind(bound))
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
            assert reads("n_").search(plan(statement.text))
        assert reads("n").search(plan(backend.held_properties(model).text))
        assert reads("n").search(plan(backend.found_stamp(model).text))
    finally:
        gom.indexes.drop(index)


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


def test_a_load_of_a_date_id_declared_text_is_refused_by_name(gom: StatelessGraphObjectManager):
    # The id would load as its text, which a lookup never finds: a save of it would make a second node (#560).
    gom.save(LookupDay(id=date(2026, 1, 5), name="day"))
    with pytest.raises(DrivineError, match=REFUSED):
        gom.load_all(LookupDayAsText)
    assert [it.id for it in gom.load_all(LookupDay)] == [date(2026, 1, 5)]


def test_a_load_of_a_date_id_declared_text_with_a_stamp_is_refused_by_name(gom: StatelessGraphObjectManager):
    # Before, its save was refused as stale, as though the node had been deleted.
    gom.save(LookupDay(id=date(2026, 1, 5), name="day"))
    with pytest.raises(DrivineError, match=r"LookupDayAsStampedText's NodeId id is declared a str, .* as a date"):
        gom.load_all(LookupDayAsStampedText)


def test_a_datetime_id_declared_text_is_refused_with_its_kind(gom: StatelessGraphObjectManager):
    gom.save(LookupDayAt(id=datetime(2026, 1, 5, 10, tzinfo=UTC), name="at ten"))
    with pytest.raises(DrivineError, match=r"holds it as a datetime: declare id as a datetime to load it\."):
        gom.load_all(LookupDayAsText)


def test_a_related_node_whose_date_id_is_declared_text_is_refused_by_name(gom: StatelessGraphObjectManager):
    gom.save(LookupDay(id=date(2026, 1, 5), name="day"))
    assert gom.edges.relate(TARGET, node_ref(LookupDay, date(2026, 1, 5)), "ON_TEXT_DAY")
    with pytest.raises(DrivineError, match=REFUSED):
        gom.load_all(LookupTargetTextDays)
