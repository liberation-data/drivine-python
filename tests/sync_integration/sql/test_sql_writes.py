# Written by scripts/unasync.py from tests/integration/sql/test_sql_writes.py:
# change that, and run the script again.
import re
from collections.abc import Iterator
from typing import Annotated

import pytest

from drivine.sync import (
    CascadeType,
    Direction,
    GraphView,
    NodeFragment,
    NodeId,
    NodeStamp,
    PatchResult,
    PersistenceManager,
    RelateMode,
    Relationship,
    Root,
    StatelessGraphObjectManager,
    node_ref,
    refs,
)
from tests.sync_integration.sql.sql_seed import SqlSeed, raw_property

STAMP = re.compile(r"^[0-9a-f]{16}:[0-9a-f]{16}$")


class Session(NodeFragment, labels=["SqlWrite", "Session"]):
    id: Annotated[str, NodeId()]
    last_activity: int | None = None
    summary: str | None = None
    stamp: Annotated[str | None, NodeStamp()] = None


class Message(NodeFragment, labels=["SqlWrite", "Message"]):
    id: Annotated[str, NodeId()]
    text: str | None = None
    stamp: Annotated[str | None, NodeStamp()] = None


class Person(NodeFragment, labels=["SqlWrite", "Person"]):
    id: Annotated[str, NodeId()]
    name: str | None = None


class Conversation(GraphView):
    session: Annotated[Session, Root()]
    messages: Annotated[list[Message], Relationship("HAS_MESSAGE")] = []
    owner: Annotated[Person | None, Relationship("OWNED_BY")] = None


class Pairing(GraphView):
    """People paired either way round with the root: a view whose cascade follows a relationship either way."""

    person: Annotated[Person, Root()]
    pairs: Annotated[list[Person], Relationship("PAIRED", Direction.UNDIRECTED)] = []


class Team(GraphView):
    """People a root leads, is mentored by and is paired with: a cascade's steps out, in and either way."""

    person: Annotated[Person, Root()]
    led: Annotated[list[Person], Relationship("LEADS")] = []
    mentors: Annotated[list[Person], Relationship("MENTORS", Direction.INCOMING)] = []
    pairs: Annotated[list[Person], Relationship("PAIRED", Direction.UNDIRECTED)] = []


@pytest.fixture(autouse=True)
def cleared(pm: PersistenceManager) -> Iterator[None]:
    seed = SqlSeed(pm, "SqlWrite")
    seed.clear()
    yield
    seed.clear()


def test_saves_fragments_of_several_classes_in_one_batch_handing_back_stamps(gom: StatelessGraphObjectManager):
    objects = [Session(id="s1", summary="One"), Session(id="s2"), Message(id="m1", text="Hi")]
    gom.save_all(objects)
    assert all(it.stamp is not None and STAMP.match(it.stamp) for it in objects)
    assert (gom.load(Session, "s1")).summary == "One"
    assert (gom.load(Message, "m1")).stamp == objects[2].stamp


def test_hands_each_fragment_its_own_stamp_when_given_out_of_the_order_they_are_written_in(
    gom: StatelessGraphObjectManager,
):
    gom.save(Message(id="m1", text="Before"))
    m1 = gom.load(Message, "m1")
    m1.text = "One"
    objects = [Message(id="m2", text="Two"), Session(id="s1"), m1]
    gom.save_all(objects)
    assert objects[0].stamp != objects[2].stamp
    assert [it.id for it in objects] == ["m2", "s1", "m1"]
    assert objects[0].stamp == (gom.load(Message, "m2")).stamp
    assert objects[2].stamp == (gom.load(Message, "m1")).stamp
    assert objects[1].stamp == (gom.load(Session, "s1")).stamp


def test_patches_where_a_condition_holds_with_all_four_outcomes(gom: StatelessGraphObjectManager):
    gom.save(Session(id="s1", last_activity=100))
    q = refs(Session)

    def advance(id: str, to: int) -> PatchResult:
        return gom.patch(Session, id, set={"last_activity": to}, where=q.last_activity.lt(to))

    assert advance("nobody", 200) == PatchResult(False, False, False)
    before = (gom.load(Session, "s1")).stamp
    assert advance("s1", 200) == PatchResult(True, True, True)
    assert advance("s1", 150) == PatchResult(True, False, False)
    assert gom.patch(Session, "s1", set={"last_activity": 200}) == PatchResult(True, True, False)
    after = gom.load(Session, "s1")
    assert after.last_activity == 200
    assert after.stamp.split(":")[0] != before.split(":")[0]
    gom.patch(Session, "s1", clear=["last_activity"])
    assert (gom.load(Session, "s1")).last_activity is None


def test_patches_where_a_text_condition_holds_with_its_quotes_taken_as_text(gom: StatelessGraphObjectManager):
    gom.save(Session(id="s1", summary="O'Brien's"))
    q = refs(Session)
    assert gom.patch(Session, "s1", set={"last_activity": 1}, where=q.summary.eq("O'Brien")) == PatchResult(
        True, False, False
    )
    assert gom.patch(Session, "s1", set={"last_activity": 2}, where=q.summary.eq("O'Brien's")) == PatchResult(
        True, True, True
    )
    assert (gom.load(Session, "s1")).last_activity == 2


def test_patches_where_a_raw_condition_holds_a_negative_number_bound_in_it(
    pm: PersistenceManager, gom: StatelessGraphObjectManager
):
    gom.save(Session(id="s1", last_activity=100))
    where = f"{raw_property(pm, 'lastActivity', 'NUMBER')} > 150-$back"
    assert gom.patch(Session, "s1", set={"summary": "patched"}, where=where, bindings={"back": -60}) == (
        PatchResult(True, False, False)
    )
    assert gom.patch(Session, "s1", set={"summary": "patched"}, where=where, bindings={"back": 60}) == (
        PatchResult(True, True, True)
    )


def test_patches_many_in_order_making_none(gom: StatelessGraphObjectManager):
    gom.save_all([Session(id="s1", summary="a"), Session(id="s2", summary="b")])
    results = gom.patch_all(
        [Session(id="s2", summary="B"), Session(id="ghost", summary="x"), Session(id="s1", summary="a")],
        only=["summary"],
    )
    assert results == [PatchResult(True, True, True), PatchResult(False, False, False), PatchResult(True, True, False)]
    assert (gom.load(Session, "s2")).summary == "B"
    assert gom.load(Session, "ghost") is None


def test_updates_by_loading_changing_and_saving(gom: StatelessGraphObjectManager):
    gom.save(Session(id="s1", summary="old"))

    def renew(it: Session) -> None:
        it.summary = "new"

    updated = gom.update(Session, "s1", renew)
    assert updated is not None and updated.summary == "new"
    assert (gom.load(Session, "s1")).summary == "new"


def test_relates_unrelates_and_loads_related_nodes_marking_both_ends(gom: StatelessGraphObjectManager):
    gom.save_all([Session(id="s1"), Message(id="m1"), Message(id="m2")])
    s1, m1 = node_ref(Session, "s1"), node_ref(Message, "m1")

    def stamp() -> str:
        return (gom.load(Session, "s1")).stamp

    first = stamp()
    assert gom.edges.relate(s1, m1, "HAS_MESSAGE", {"order": 1})
    second = stamp()
    assert second.split(":")[1] != first.split(":")[1]
    assert gom.edges.relate(s1, m1, "HAS_MESSAGE", {"order": 1})
    assert stamp() == second
    gom.edges.relate(s1, node_ref(Message, "m2"), "HAS_MESSAGE", {}, RelateMode.CREATE)
    assert not gom.edges.relate(s1, node_ref(Message, "nobody"), "HAS_MESSAGE")
    related = gom.edges.load_related(s1, "HAS_MESSAGE", Message)
    assert sorted(it.id for it in related) == ["m1", "m2"]
    back = gom.edges.load_related(m1, "HAS_MESSAGE", Session, Direction.INCOMING)
    assert [it.id for it in back] == ["s1"]
    assert gom.edges.unrelate(s1, m1, "HAS_MESSAGE") == 1
    assert gom.edges.unrelate_all(s1, "HAS_MESSAGE") == 1
    assert gom.edges.unrelate(s1, node_ref(Message, "nobody"), "HAS_MESSAGE") == 0
    assert gom.edges.unrelate_all(node_ref(Session, "nobody"), "HAS_MESSAGE") == 0
    assert gom.edges.load_related(s1, "HAS_MESSAGE", Message) == []


def test_loads_the_nodes_related_either_way_round_one_related_to_itself_once(gom: StatelessGraphObjectManager):
    # (#390)
    gom.save_all([Person(id=it) for it in ["p1", "p2", "p3", "p4"]])
    for origin, to in [("p1", "p2"), ("p3", "p1"), ("p1", "p1"), ("p2", "p4")]:
        gom.edges.relate(node_ref(Person, origin), node_ref(Person, to), "KNOWS")

    def known(id: str) -> list[str]:
        return sorted(
            it.id for it in gom.edges.load_related(node_ref(Person, id), "KNOWS", Person, Direction.UNDIRECTED)
        )

    assert known("p1") == ["p1", "p2", "p3"]
    assert known("p2") == ["p1", "p4"]
    assert known("p4") == ["p2"]


def _relate(gom: StatelessGraphObjectManager, origin: str, to: str, type: str) -> bool:
    return gom.edges.relate(node_ref(Person, origin), node_ref(Person, to), type)


def _left(gom: StatelessGraphObjectManager, ids: list[str]) -> dict[str, bool]:
    return {id: gom.load(Person, id) is not None for id in ids}


def test_keeps_in_a_cascade_a_node_one_step_either_way_round_from_a_kept_one(gom: StatelessGraphObjectManager):
    # (#390)
    ids = ["root", "a", "b", "c", "outside"]
    gom.save_all([Person(id=it) for it in ids])
    # The view reaches a, b and c from the root, b stored the other way round.
    _relate(gom, "root", "a", "PAIRED")
    _relate(gom, "b", "root", "PAIRED")
    _relate(gom, "root", "c", "PAIRED")
    _relate(gom, "c", "c", "PAIRED")
    # a is kept by a node outside the cascade, and b by its pairing with a, stored the other way round from a.
    _relate(gom, "a", "outside", "KNOWS")
    _relate(gom, "b", "a", "PAIRED")
    # Every node deleted (#375): root and c.
    assert gom.delete(Pairing, "root", cascade=CascadeType.DELETE_ORPHAN) == 2
    assert _left(gom, ids) == {"root": False, "a": True, "b": True, "c": False, "outside": True}


def test_keeps_in_a_cascade_that_steps_either_way_a_node_by_a_step_out_or_in_as_stored(
    gom: StatelessGraphObjectManager,
):
    # (#390)
    ids = ["root", "a", "b", "c", "d", "f", "g", "outside"]
    gom.save_all([Person(id=it) for it in ids])
    # The view reaches a, b and c led by the root, d and f its mentors, and g paired with it.
    for it in ["a", "b", "c"]:
        _relate(gom, "root", it, "LEADS")
    _relate(gom, "d", "root", "MENTORS")
    _relate(gom, "f", "root", "MENTORS")
    _relate(gom, "root", "g", "PAIRED")
    # a is kept by a node outside the cascade. From a, b is led and d a mentor, so both are kept; c leads a and f is
    # mentored by a, each stored the other way round from a step of the view, so neither is.
    _relate(gom, "a", "outside", "KNOWS")
    _relate(gom, "a", "b", "LEADS")
    _relate(gom, "d", "a", "MENTORS")
    _relate(gom, "c", "a", "LEADS")
    _relate(gom, "a", "f", "MENTORS")
    # Every node deleted (#375): root, c, f and g.
    assert gom.delete(Team, "root", cascade=CascadeType.DELETE_ORPHAN) == 4
    assert _left(gom, ids) == {
        "root": False,
        "a": True,
        "b": True,
        "c": False,
        "d": True,
        "f": False,
        "g": False,
        "outside": True,
    }


def test_cascades_a_delete_to_every_node_the_view_reaches_or_to_orphans(gom: StatelessGraphObjectManager):
    def conversation(id: str, messages: list[str], owner: str) -> Conversation:
        return Conversation(
            session=Session(id=id), messages=[Message(id=it) for it in messages], owner=Person(id=owner)
        )

    gom.save(conversation("s1", ["m1", "m2"], "ada"))
    gom.save(conversation("s2", ["m3"], "ada"))
    # Every node deleted (#375): s2 and m3, ada kept; then s1, m1, m2 and ada.
    assert gom.delete(Conversation, "s2", cascade=CascadeType.DELETE_ORPHAN) == 2
    assert gom.load(Message, "m3") is None
    assert gom.load(Person, "ada") is not None
    assert gom.delete(Conversation, "s1", cascade=CascadeType.DELETE_ALL) == 4
    assert gom.load(Message, "m1") is None
    assert gom.load(Person, "ada") is None
