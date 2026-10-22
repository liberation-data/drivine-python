import asyncio
import re
from collections.abc import AsyncIterator
from typing import Annotated, Any

import pytest

from drivine import (
    DatabaseType,
    Direction,
    GraphProperty,
    GraphView,
    NodeFragment,
    NodeId,
    NodeLabels,
    NodeStamp,
    NullPolicy,
    PersistenceManager,
    PropertyBag,
    QuerySpecification,
    Relationship,
    RelationshipFragment,
    RemovedTargets,
    Root,
    StaleObjectError,
    TargetNode,
)
from tests.integration.sql.sql_seed import SqlSeed, raw_props

STAMP = re.compile(r"^[0-9a-f]{16}:[0-9a-f]{16}$")


class Person(NodeFragment, labels=["SqlSave", "Person"]):
    id: Annotated[str, NodeId()]
    name: str | None = None
    born: Annotated[int | None, GraphProperty("born_in")] = None
    stamp: Annotated[str | None, NodeStamp()] = None
    metadata: Annotated[dict[str, Any], PropertyBag()] = {}
    tags: Annotated[list[str], NodeLabels()] = []


class Pilot(Person, labels=["Pilot"]):
    pass


class Tag(NodeFragment, labels=["SqlSave", "Tag"]):
    id: Annotated[str, NodeId()]
    stamp: Annotated[str | None, NodeStamp()] = None


class Note(NodeFragment, labels=["SqlSave", "Note"]):
    """A bag and no NodeLabels: a save merges its properties in place, as Oracle then keeps them (OSON version 2)."""

    id: Annotated[str, NodeId()]
    metadata: Annotated[dict[str, Any], PropertyBag()] = {}


class Org(NodeFragment, labels=["SqlSave", "Org"]):
    id: Annotated[str, NodeId()]
    name: str | None = None


class Job(RelationshipFragment):
    role: str | None = None
    """Optional: a Team's save joins its members to the org with no role."""
    org: Annotated[Org, TargetNode()]


class Career(GraphView):
    person: Annotated[Person, Root()]
    tag_list: Annotated[list[Tag], Relationship("TAGGED")] = []
    jobs: Annotated[list[Job], Relationship("WORKS_FOR")] = []
    mentor: Annotated[Person | None, Relationship("MENTORED_BY")] = None


class Team(GraphView):
    org: Annotated[Org, Root()]
    members: Annotated[list[Career], Relationship("WORKS_FOR", Direction.INCOMING)] = []


@pytest.fixture(autouse=True)
async def cleared(pm: PersistenceManager) -> AsyncIterator[None]:
    seed = SqlSeed(pm, "SqlSave")
    await seed.prepare()
    await seed.clear()
    yield
    await seed.clear()


async def test_saves_hands_back_a_whole_stamp_and_keeps_the_data_token_when_nothing_changed(pm, gom):
    ada = await gom.save(Person(id="ada", name="Ada", born=1815, metadata={"source": "wiki"}))
    assert ada.stamp is not None and STAMP.match(ada.stamp)
    loaded = await gom.load(Person, "ada")
    assert loaded == Person(id="ada", name="Ada", born=1815, stamp=ada.stamp, metadata={"source": "wiki"})
    again = await gom.save(loaded)
    assert again.stamp == ada.stamp
    loaded.name = "Ada Lovelace"
    changed = await gom.save(loaded)
    assert changed.stamp.split(":")[0] != ada.stamp.split(":")[0]
    assert changed.stamp.split(":")[1] == ada.stamp.split(":")[1]
    raw = await pm.get_one(QuerySpecification[Any](raw_props(pm, "SqlSave")))
    assert raw.get("props", raw) == {
        "id": "ada",
        "name": "Ada Lovelace",
        "born_in": 1815,
        "metadata.source": "wiki",
        # An open label field's record of the labels it holds: drivine4j's write function removes one that is empty.
        **({"__drivine.labels.tags": []} if pm.type is DatabaseType.ORACLE else {}),
    }


async def test_refuses_a_stale_save_and_one_of_a_deleted_node_writing_nothing(gom):
    mine = await gom.save(Person(id="bob", name="Bob"))
    theirs = await gom.load(Person, "bob")
    theirs.name = "Theirs"
    await gom.save(theirs)
    mine.name = "Mine"
    with pytest.raises(StaleObjectError) as refusal:
        await gom.save(mine)
    assert not refusal.value.deleted
    assert (await gom.load(Person, "bob")).name == "Theirs"
    await gom.delete(Person, "bob")
    with pytest.raises(StaleObjectError) as gone:
        await gom.save(mine)
    assert gone.value.deleted
    assert await gom.load(Person, "bob") is None


@pytest.mark.async_only
async def test_exactly_one_of_several_writers_holding_one_stamp_wins(gom):
    base = await gom.save(Person(id="cy", name="Cy"))
    writers = [Person(id="cy", name=f"Writer {i}", stamp=base.stamp) for i in range(1, 6)]
    results = await asyncio.gather(*(gom.save(it) for it in writers), return_exceptions=True)
    assert sum(not isinstance(it, BaseException) for it in results) == 1
    assert all(isinstance(it, StaleObjectError) for it in results if isinstance(it, BaseException))


async def test_creates_only_what_is_not_there(gom):
    assert await gom.create(Person(id="dee", name="Dee")) is not None
    assert await gom.create(Person(id="dee", name="Other")) is None
    assert (await gom.load(Person, "dee")).name == "Dee"


async def test_clears_nulls_under_clear_and_writes_only_the_fields_named(gom):
    await gom.save(Person(id="eve", name="Eve", born=1990))
    await gom.save(Person(id="eve", name=None), null_policy=NullPolicy.CLEAR, only=["name"])
    eve = await gom.load(Person, "eve")
    assert (eve.id, eve.born, eve.name) == ("eve", 1990, None)
    await gom.save(Person(id="eve", name="Eve", born=2000), except_=["born"])
    eve = await gom.load(Person, "eve")
    assert (eve.name, eve.born) == ("Eve", 1990)


async def test_saves_the_labels_a_node_labels_field_holds_and_a_subclass_with_its_own(gom):
    await gom.save(Person(id="fay", tags=["Speaker", "Author"]))
    assert sorted((await gom.load(Person, "fay")).tags) == ["Author", "Speaker"]
    await gom.save(Person(id="fay", tags=["Speaker"]), null_policy=NullPolicy.CLEAR)
    assert (await gom.load(Person, "fay")).tags == ["Speaker"]
    await gom.save(Pilot(id="gus", name="Gus"))
    assert type(await gom.load(Person, "gus")) is Pilot


async def test_saves_a_view_with_relationships_relationship_fragments_and_a_nested_single(gom):
    saved = await gom.save(
        Career(
            person=Person(id="hal", name="Hal"),
            tag_list=[Tag(id="t1"), Tag(id="t2")],
            jobs=[Job(role="lead", org=Org(id="acme", name="Acme"))],
            mentor=Person(id="ida", name="Ida"),
        )
    )
    assert all(it.stamp is not None and ":" in it.stamp for it in saved.tag_list)
    hal = await gom.load(Career, "hal")
    assert sorted(it.id for it in hal.tag_list) == ["t1", "t2"]
    assert [(it.role, it.org.name) for it in hal.jobs] == [("lead", "Acme")]
    assert hal.mentor.name == "Ida"
    team = await gom.load(Team, "acme")
    assert [it.person.id for it in team.members] == ["hal"]
    before = hal.person.stamp
    await gom.save(hal)
    assert (await gom.load(Career, "hal")).person.stamp == before


async def test_replaces_a_list_deleting_removed_targets_nothing_else_refers_to(gom):
    await gom.save(Career(person=Person(id="jo"), tag_list=[Tag(id="t1"), Tag(id="t2"), Tag(id="t3")]))
    await gom.save(Career(person=Person(id="kim"), tag_list=[Tag(id="t3")]))
    jo = await gom.load(Career, "jo")
    loaded_stamp = jo.person.stamp
    jo.tag_list = [it for it in jo.tag_list if it.id == "t1"]
    await gom.save(jo, replace=["tag_list"], removed_targets=RemovedTargets.DELETE_UNREFERENCED)
    assert [it.id for it in (await gom.load(Career, "jo")).tag_list] == ["t1"]
    assert await gom.load(Tag, "t2") is None
    assert await gom.load(Tag, "t3") is not None
    stale = Career(person=Person(id="jo", stamp=loaded_stamp), tag_list=[])
    with pytest.raises(StaleObjectError):
        await gom.save(stale, replace=["tag_list"])


async def test_replaces_a_list_of_views_keeping_the_views_whose_roots_the_list_still_holds(gom):
    acme = Org(id="acme", name="Acme")
    await gom.save(Team(org=acme, members=[Career(person=Person(id="lu")), Career(person=Person(id="mo"))]))
    team = await gom.load(Team, "acme")
    team.members = [it for it in team.members if it.person.id == "mo"]
    await gom.save(team, replace=["members"])
    assert [it.person.id for it in (await gom.load(Team, "acme")).members] == ["mo"]


async def test_saves_a_batch_whose_stamps_run_past_4000_characters(gom):
    await gom.save_all([Tag(id=f"t{i}") for i in range(300)])
    assert await gom.count(Tag) == 300


async def test_clears_under_clear_a_node_saved_more_than_once_as_oracle_then_keeps_it(gom):
    await gom.save(Note(id="n1", metadata={"source": "wiki", "rank": 1}))
    await gom.save(Note(id="n1", metadata={"source": "wiki", "rank": 2}))
    await gom.save(Note(id="n1", metadata={"rank": 3}), null_policy=NullPolicy.CLEAR)
    assert (await gom.load(Note, "n1")).metadata == {"rank": 3}
