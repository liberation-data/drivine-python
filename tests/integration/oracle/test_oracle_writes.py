import re
from collections.abc import AsyncIterator
from typing import Annotated

import pytest

from drivine import (
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
from tests.integration.oracle.oracle_seed import ON_ORACLE, OracleSeed

STAMP = re.compile(r"^[0-9a-f]{16}:[0-9a-f]{16}$")


class Session(NodeFragment, labels=["OraWrite", "Session"]):
    id: Annotated[str, NodeId()]
    last_activity: int | None = None
    summary: str | None = None
    stamp: Annotated[str | None, NodeStamp()] = None


class Message(NodeFragment, labels=["OraWrite", "Message"]):
    id: Annotated[str, NodeId()]
    text: str | None = None
    stamp: Annotated[str | None, NodeStamp()] = None


class Person(NodeFragment, labels=["OraWrite", "Person"]):
    id: Annotated[str, NodeId()]
    name: str | None = None


class Conversation(GraphView):
    session: Annotated[Session, Root()]
    messages: Annotated[list[Message], Relationship("HAS_MESSAGE")] = []
    owner: Annotated[Person | None, Relationship("OWNED_BY")] = None


@pytest.fixture(autouse=True)
async def cleared(pm: PersistenceManager) -> AsyncIterator[None]:
    seed = OracleSeed(pm, "OraWrite")
    await seed.clear()
    yield
    await seed.clear()


pytestmark = ON_ORACLE


async def test_saves_fragments_of_several_classes_in_one_batch_handing_back_stamps(gom: StatelessGraphObjectManager):
    objects = [Session(id="s1", summary="One"), Session(id="s2"), Message(id="m1", text="Hi")]
    await gom.save_all(objects)
    assert all(it.stamp is not None and STAMP.match(it.stamp) for it in objects)
    assert (await gom.load(Session, "s1")).summary == "One"
    assert (await gom.load(Message, "m1")).stamp == objects[2].stamp


async def test_patches_where_a_condition_holds_with_all_four_outcomes(gom: StatelessGraphObjectManager):
    await gom.save(Session(id="s1", last_activity=100))
    q = refs(Session)

    async def advance(id: str, to: int) -> PatchResult:
        return await gom.patch(Session, id, set={"last_activity": to}, where=q.last_activity.lt(to))

    assert await advance("nobody", 200) == PatchResult(False, False, False)
    before = (await gom.load(Session, "s1")).stamp
    assert await advance("s1", 200) == PatchResult(True, True, True)
    assert await advance("s1", 150) == PatchResult(True, False, False)
    assert await gom.patch(Session, "s1", set={"last_activity": 200}) == PatchResult(True, True, False)
    after = await gom.load(Session, "s1")
    assert after.last_activity == 200
    assert after.stamp.split(":")[0] != before.split(":")[0]
    await gom.patch(Session, "s1", clear=["last_activity"])
    assert (await gom.load(Session, "s1")).last_activity is None


async def test_patches_many_in_order_making_none(gom: StatelessGraphObjectManager):
    await gom.save_all([Session(id="s1", summary="a"), Session(id="s2", summary="b")])
    results = await gom.patch_all(
        [Session(id="s2", summary="B"), Session(id="ghost", summary="x"), Session(id="s1", summary="a")],
        only=["summary"],
    )
    assert results == [PatchResult(True, True, True), PatchResult(False, False, False), PatchResult(True, True, False)]
    assert (await gom.load(Session, "s2")).summary == "B"
    assert await gom.load(Session, "ghost") is None


async def test_updates_by_loading_changing_and_saving(gom: StatelessGraphObjectManager):
    await gom.save(Session(id="s1", summary="old"))

    def renew(it: Session) -> None:
        it.summary = "new"

    updated = await gom.update(Session, "s1", renew)
    assert updated is not None and updated.summary == "new"
    assert (await gom.load(Session, "s1")).summary == "new"


async def test_relates_unrelates_and_loads_related_nodes_marking_both_ends(gom: StatelessGraphObjectManager):
    await gom.save_all([Session(id="s1"), Message(id="m1"), Message(id="m2")])
    s1, m1 = node_ref(Session, "s1"), node_ref(Message, "m1")

    async def stamp() -> str:
        return (await gom.load(Session, "s1")).stamp

    first = await stamp()
    assert await gom.edges.relate(s1, m1, "HAS_MESSAGE", {"order": 1})
    second = await stamp()
    assert second.split(":")[1] != first.split(":")[1]
    assert await gom.edges.relate(s1, m1, "HAS_MESSAGE", {"order": 1})
    assert await stamp() == second
    await gom.edges.relate(s1, node_ref(Message, "m2"), "HAS_MESSAGE", {}, RelateMode.CREATE)
    assert not await gom.edges.relate(s1, node_ref(Message, "nobody"), "HAS_MESSAGE")
    related = await gom.edges.load_related(s1, "HAS_MESSAGE", Message)
    assert sorted(it.id for it in related) == ["m1", "m2"]
    back = await gom.edges.load_related(m1, "HAS_MESSAGE", Session, Direction.INCOMING)
    assert [it.id for it in back] == ["s1"]
    assert await gom.edges.unrelate(s1, m1, "HAS_MESSAGE") == 1
    assert await gom.edges.unrelate_all(s1, "HAS_MESSAGE") == 1
    assert await gom.edges.load_related(s1, "HAS_MESSAGE", Message) == []


async def test_cascades_a_delete_to_every_node_the_view_reaches_or_to_orphans(gom: StatelessGraphObjectManager):
    def conversation(id: str, messages: list[str], owner: str) -> Conversation:
        return Conversation(
            session=Session(id=id), messages=[Message(id=it) for it in messages], owner=Person(id=owner)
        )

    await gom.save(conversation("s1", ["m1", "m2"], "ada"))
    await gom.save(conversation("s2", ["m3"], "ada"))
    assert await gom.delete(Conversation, "s2", cascade=CascadeType.DELETE_ORPHAN) == 1
    assert await gom.load(Message, "m3") is None
    assert await gom.load(Person, "ada") is not None
    assert await gom.delete(Conversation, "s1", cascade=CascadeType.DELETE_ALL) == 1
    assert await gom.load(Message, "m1") is None
    assert await gom.load(Person, "ada") is None
