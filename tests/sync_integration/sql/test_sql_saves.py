# Written by scripts/unasync.py from tests/integration/sql/test_sql_saves.py:
# change that, and run the script again.
import re
from collections.abc import Iterator
from typing import Annotated, Any

import pytest

from drivine.sync import (
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
from tests.sync_integration.sql.sql_seed import SqlSeed, raw_props

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
def cleared(pm: PersistenceManager) -> Iterator[None]:
    seed = SqlSeed(pm, "SqlSave")
    seed.clear()
    yield
    seed.clear()


def test_saves_hands_back_a_whole_stamp_and_keeps_the_data_token_when_nothing_changed(pm, gom):
    ada = gom.save(Person(id="ada", name="Ada", born=1815, metadata={"source": "wiki"}))
    assert ada.stamp is not None and STAMP.match(ada.stamp)
    loaded = gom.load(Person, "ada")
    assert loaded == Person(id="ada", name="Ada", born=1815, stamp=ada.stamp, metadata={"source": "wiki"})
    again = gom.save(loaded)
    assert again.stamp == ada.stamp
    loaded.name = "Ada Lovelace"
    changed = gom.save(loaded)
    assert changed.stamp.split(":")[0] != ada.stamp.split(":")[0]
    assert changed.stamp.split(":")[1] == ada.stamp.split(":")[1]
    raw = pm.get_one(QuerySpecification[Any](raw_props(pm, "SqlSave")))
    assert raw.get("props", raw) == {
        "id": "ada",
        "name": "Ada Lovelace",
        "born_in": 1815,
        "metadata.source": "wiki",
        # An open label field's record of the labels it holds: drivine4j's write function removes one that is empty.
        **({"__drivine.labels.tags": []} if pm.type is DatabaseType.ORACLE else {}),
    }


def test_refuses_a_stale_save_and_one_of_a_deleted_node_writing_nothing(gom):
    mine = gom.save(Person(id="bob", name="Bob"))
    theirs = gom.load(Person, "bob")
    theirs.name = "Theirs"
    gom.save(theirs)
    mine.name = "Mine"
    with pytest.raises(StaleObjectError) as refusal:
        gom.save(mine)
    assert not refusal.value.deleted
    assert (gom.load(Person, "bob")).name == "Theirs"
    gom.delete(Person, "bob")
    with pytest.raises(StaleObjectError) as gone:
        gom.save(mine)
    assert gone.value.deleted
    assert gom.load(Person, "bob") is None


def test_creates_only_what_is_not_there(gom):
    assert gom.create(Person(id="dee", name="Dee")) is not None
    assert gom.create(Person(id="dee", name="Other")) is None
    assert (gom.load(Person, "dee")).name == "Dee"


def test_clears_nulls_under_clear_and_writes_only_the_fields_named(gom):
    gom.save(Person(id="eve", name="Eve", born=1990))
    gom.save(Person(id="eve", name=None), null_policy=NullPolicy.CLEAR, only=["name"])
    eve = gom.load(Person, "eve")
    assert (eve.id, eve.born, eve.name) == ("eve", 1990, None)
    gom.save(Person(id="eve", name="Eve", born=2000), except_=["born"])
    eve = gom.load(Person, "eve")
    assert (eve.name, eve.born) == ("Eve", 1990)


def test_saves_the_labels_a_node_labels_field_holds_and_a_subclass_with_its_own(gom):
    gom.save(Person(id="fay", tags=["Speaker", "Author"]))
    assert sorted((gom.load(Person, "fay")).tags) == ["Author", "Speaker"]
    gom.save(Person(id="fay", tags=["Speaker"]), null_policy=NullPolicy.CLEAR)
    assert (gom.load(Person, "fay")).tags == ["Speaker"]
    gom.save(Pilot(id="gus", name="Gus"))
    assert type(gom.load(Person, "gus")) is Pilot


def test_saves_a_view_with_relationships_relationship_fragments_and_a_nested_single(gom):
    saved = gom.save(
        Career(
            person=Person(id="hal", name="Hal"),
            tag_list=[Tag(id="t1"), Tag(id="t2")],
            jobs=[Job(role="lead", org=Org(id="acme", name="Acme"))],
            mentor=Person(id="ida", name="Ida"),
        )
    )
    assert all(it.stamp is not None and ":" in it.stamp for it in saved.tag_list)
    hal = gom.load(Career, "hal")
    assert sorted(it.id for it in hal.tag_list) == ["t1", "t2"]
    assert [(it.role, it.org.name) for it in hal.jobs] == [("lead", "Acme")]
    assert hal.mentor.name == "Ida"
    team = gom.load(Team, "acme")
    assert [it.person.id for it in team.members] == ["hal"]
    before = hal.person.stamp
    gom.save(hal)
    assert (gom.load(Career, "hal")).person.stamp == before


def test_replaces_a_list_deleting_removed_targets_nothing_else_refers_to(gom):
    gom.save(Career(person=Person(id="jo"), tag_list=[Tag(id="t1"), Tag(id="t2"), Tag(id="t3")]))
    gom.save(Career(person=Person(id="kim"), tag_list=[Tag(id="t3")]))
    jo = gom.load(Career, "jo")
    loaded_stamp = jo.person.stamp
    jo.tag_list = [it for it in jo.tag_list if it.id == "t1"]
    gom.save(jo, replace=["tag_list"], removed_targets=RemovedTargets.DELETE_UNREFERENCED)
    assert [it.id for it in (gom.load(Career, "jo")).tag_list] == ["t1"]
    assert gom.load(Tag, "t2") is None
    assert gom.load(Tag, "t3") is not None
    stale = Career(person=Person(id="jo", stamp=loaded_stamp), tag_list=[])
    with pytest.raises(StaleObjectError):
        gom.save(stale, replace=["tag_list"])


def test_replaces_a_list_of_views_keeping_the_views_whose_roots_the_list_still_holds(gom):
    acme = Org(id="acme", name="Acme")
    gom.save(Team(org=acme, members=[Career(person=Person(id="lu")), Career(person=Person(id="mo"))]))
    team = gom.load(Team, "acme")
    team.members = [it for it in team.members if it.person.id == "mo"]
    gom.save(team, replace=["members"])
    assert [it.person.id for it in (gom.load(Team, "acme")).members] == ["mo"]


def test_saves_a_batch_whose_stamps_run_past_4000_characters(gom):
    gom.save_all([Tag(id=f"t{i}") for i in range(300)])
    assert gom.count(Tag) == 300


def test_clears_under_clear_a_node_saved_more_than_once_as_oracle_then_keeps_it(gom):
    gom.save(Note(id="n1", metadata={"source": "wiki", "rank": 1}))
    gom.save(Note(id="n1", metadata={"source": "wiki", "rank": 2}))
    gom.save(Note(id="n1", metadata={"rank": 3}), null_policy=NullPolicy.CLEAR)
    assert (gom.load(Note, "n1")).metadata == {"rank": 3}


def test_hands_a_related_node_held_twice_the_stamp_it_was_left_with_as_one_the_save_made(gom):
    saved = gom.save(Career(person=Person(id="nan"), tag_list=[Tag(id="t1"), Tag(id="t1")]))
    stamp = (gom.load(Tag, "t1")).stamp
    assert [it.stamp for it in saved.tag_list] == [stamp, stamp]


def test_hands_a_node_a_batch_holds_twice_the_stamp_it_was_left_with_as_one_the_batch_made(gom):
    saved = gom.save_all([Tag(id="t1"), Tag(id="t1")])
    stamp = (gom.load(Tag, "t1")).stamp
    assert [it.stamp for it in saved] == [stamp, stamp]
