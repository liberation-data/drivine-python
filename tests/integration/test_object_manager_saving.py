import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Any, TypeVar

import pytest

from drivine import (
    CascadeType,
    DatabaseType,
    DrivineError,
    JsonPacked,
    NodeFragment,
    NodeId,
    NodeLabels,
    NodeStamp,
    NullPolicy,
    PersistenceManager,
    PropertyBag,
    QuerySpecification,
    RemovedTargets,
    StaleObjectError,
    StatelessGraphObjectManager,
    stamps,
    transaction,
)
from tests.integration.stores import equals, labels, nodes, set_property
from tests.models import (
    Animal,
    Customer,
    Location,
    LocationHierarchy,
    Organization,
    Person,
    PersonCareer,
    Puppy,
    WorkHistory,
)

T = TypeVar("T")


class Finding(NodeFragment, labels=["Finding"]):
    id: Annotated[str, NodeId()]
    sources: list[str] = []
    stamp: Annotated[str | None, NodeStamp()] = None


class Tagged(NodeFragment, labels=["Tagged"]):
    id: Annotated[str, NodeId()]
    text: str | None = None
    meta: Annotated[dict[str, Any], PropertyBag()] = {}
    stamp: Annotated[str | None, NodeStamp()] = None


class Sticker(NodeFragment, labels=["Sticker"]):
    """A flat bag and an open set of labels beside a stamp."""

    id: Annotated[str, NodeId()]
    labels: Annotated[list[str], NodeLabels()] = []
    extras: Annotated[dict[str, Any], PropertyBag(flat=True)] = {}
    stamp: Annotated[str | None, NodeStamp()] = None


class Shade(StrEnum):
    RED = "Red"
    GREEN = "Green"
    BLUE = "Blue"


class Swatch(NodeFragment, labels=["Swatch"]):
    """A closed set of labels: the field speaks for the Shade labels and no other."""

    id: Annotated[str, NodeId()]
    shades: Annotated[set[Shade], NodeLabels(Shade)] = set()
    stamp: Annotated[str | None, NodeStamp()] = None


class Packed(NodeFragment, labels=["Packed"]):
    id: Annotated[str, NodeId()]
    text: str | None = None
    prefs: Annotated[dict[str, Any], JsonPacked()] = {}
    tags: Annotated[list[str], JsonPacked()] = []
    stamp: Annotated[str | None, NodeStamp()] = None


class Reading(NodeFragment, labels=["Reading"]):
    """Fields of the kinds a driver hands back in a form of its own, compared as loaded."""

    id: Annotated[str, NodeId()]
    taken: datetime
    on: date
    shade: Shade
    value: float
    count: int
    pair: tuple[str, str]
    stamp: Annotated[str | None, NodeStamp()] = None


class Pet(NodeFragment, labels=["Pet"]):
    id: Annotated[str, NodeId()]
    name: str
    labels: Annotated[list[str], NodeLabels()] = []
    extras: Annotated[dict[str, Any], PropertyBag(flat=True)] = {}
    stamp: Annotated[str | None, NodeStamp()] = None


class Hound(Pet, labels=["Hound"]):
    breed: str | None = None


@pytest.fixture
def gom(pm: PersistenceManager) -> StatelessGraphObjectManager:
    return StatelessGraphObjectManager(pm)


async def stored(pm: PersistenceManager, label: str, *props: str) -> object:
    """A property of the one node with the label, or a list of several, as the store holds them."""
    [node] = await nodes(pm, label)
    return node.get(props[0]) if len(props) == 1 else [node.get(it) for it in props]


def acme() -> Organization:
    return Organization(uuid="o1", name="Acme")


def babbage() -> Organization:
    return Organization(uuid="o2", name="Babbage & Co")


class TestFragments:
    async def test_saves_and_hands_back_a_whole_stamp(self, gom):
        ada = await gom.save(Person(uuid="p1", name="Ada", bio="Engineer"))
        assert ada.stamp is not None and len(ada.stamp) == 2 * stamps.TOKEN + 1
        assert await gom.load(Person, "p1") == ada

    async def test_refuses_a_stale_save_and_writes_nothing(self, pm, gom):
        await gom.save(Person(uuid="p1", name="Ada"))
        first = await gom.load(Person, "p1")
        second = await gom.load(Person, "p1")
        assert first is not None and second is not None
        second.name = "Ada L"
        await gom.save(second)
        first.name = "Countess"
        with pytest.raises(StaleObjectError) as raised:
            await gom.save(first)
        assert not raised.value.deleted and raised.value.found_stamp == second.stamp
        assert await stored(pm, "Person", "name") == "Ada L"

    async def test_a_save_of_a_deleted_node_is_stale(self, gom):
        await gom.save(Person(uuid="p1", name="Ada"))
        loaded = await gom.load(Person, "p1")
        await gom.delete(Person, "p1")
        with pytest.raises(StaleObjectError) as raised:
            await gom.save(loaded)
        assert raised.value.deleted

    async def test_null_policies(self, pm, gom):
        await gom.save(Person(uuid="p1", name="Ada", bio="Engineer"))
        await gom.save(Person(uuid="p1", name="Ada"))  # IGNORE: bio left as it is
        assert await stored(pm, "Person", "bio") == "Engineer"
        await gom.save(Person(uuid="p1", name="Ada"), null_policy=NullPolicy.CLEAR)  # bio never assigned: left
        assert await stored(pm, "Person", "bio") == "Engineer"
        await gom.save(Person(uuid="p1", name="Ada", bio=None), null_policy=NullPolicy.CLEAR)  # assigned None: cleared
        assert await stored(pm, "Person", "bio") is None

    async def test_only_and_except(self, pm, gom):
        await gom.save(Person(uuid="p1", name="Ada", bio="Engineer"))
        await gom.save(Person(uuid="p1", name="Grace", bio="Admiral"), only=["bio"])
        assert await stored(pm, "Person", "name", "bio") == ["Ada", "Admiral"]
        await gom.save(Person(uuid="p1", name="Grace", bio="Rear admiral"), except_=["bio"])
        assert await stored(pm, "Person", "name", "bio") == ["Grace", "Admiral"]
        with pytest.raises(DrivineError, match="not both"):
            await gom.save(Person(uuid="p1", name="x"), only=["name"], except_=["bio"])

    async def test_bags_labels_and_packed_values_round_trip(self, gom):
        customer = Customer(
            uuid="c1",
            full_name="Ada L",
            tags=["vip"],
            preferences={"theme": "dark", "sizes": [1, 2]},
            metadata={"source": "wiki", "score": 3},
            tiers={"Gold"},
            legacy_code="X-1",
        )
        await gom.save(customer)
        loaded = await gom.load(Customer, "c1")
        assert loaded is not None
        assert loaded.model_dump(exclude={"extras"}) == customer.model_dump(exclude={"extras"})

    @pytest.mark.parametrize("key", ["fullName", "uuid", "LEGACY", "metadata.source", "__drivine.stamp"])
    async def test_refuses_a_flat_bag_entry_a_field_another_bag_or_the_stamp_is_stored_under(self, pm, gom, key):
        customer = Customer(uuid="c1", full_name="Ada L", extras={key: "x"})
        with pytest.raises(DrivineError, match=f"Customer.extras has an entry '{key}', which is the property of a"):
            await gom.save(customer)
        with pytest.raises(DrivineError, match="which is the property of a declared field"):
            await gom.save_all([customer])
        with pytest.raises(DrivineError, match="which is the property of a declared field"):
            await gom.save(customer, only=["extras"])
        assert await nodes(pm, "Customer") == []

    async def test_a_flat_bag_takes_an_entry_named_like_a_field_stored_under_another_name(self, gom):
        await gom.save(Customer(uuid="c1", full_name="Ada L", extras={"full_name": "open", "size": 3}))
        loaded = await gom.load(Customer, "c1")
        assert loaded is not None
        assert (loaded.full_name, loaded.extras) == ("Ada L", {"full_name": "open", "size": 3})

    async def test_a_flat_bag_stores_a_key_that_isnt_a_string_under_its_text(self, gom):
        await gom.save(Customer.model_construct(uuid="c1", full_name="Ada L", extras={1: "x"}))
        loaded = await gom.load(Customer, "c1")
        assert loaded is not None
        assert loaded.extras == {"1": "x"}

    async def test_a_subclass_is_written_with_its_own_labels(self, pm, gom):
        await gom.save(Puppy(uuid="a1", name="Bit", age_in_weeks=9))
        assert await labels(pm, "a1") == ["Animal", "Dog", "Puppy"]
        assert type(await gom.load(Animal, "a1")) is Puppy

    async def test_create_makes_only_what_is_absent(self, pm, gom):
        assert await gom.create(Person(uuid="p1", name="Ada")) is not None
        assert await gom.create(Person(uuid="p1", name="Imposter")) is None
        assert await stored(pm, "Person", "name") == "Ada"
        loaded = await gom.load(Person, "p1")
        with pytest.raises(DrivineError, match="carries a stamp was loaded"):
            await gom.create(loaded)


class TestViews:
    async def test_adds_relationships_and_related_nodes(self, pm, gom):
        career = PersonCareer(
            person=Person(uuid="p1", name="Ada"),
            employment_history=[WorkHistory(start_date="2015", role="Engineer", target=babbage())],
        )
        await gom.save(career)
        career.employment_history.append(WorkHistory(start_date="2020", role="Lead", target=acme()))
        saved = await gom.save(career)
        loaded = await gom.load(PersonCareer, "p1")
        assert loaded is not None
        assert [(it.role, it.target.name) for it in loaded.employment_history] == [
            ("Engineer", "Babbage & Co"),
            ("Lead", "Acme"),
        ]
        assert loaded.person.stamp == saved.person.stamp

    async def test_a_save_without_replace_removes_nothing(self, gom):
        await gom.save(
            PersonCareer(
                person=Person(uuid="p1", name="Ada"),
                employment_history=[WorkHistory(start_date="2015", role="Engineer", target=babbage())],
            )
        )
        await gom.save(PersonCareer(person=Person(uuid="p1", name="Ada"), employment_history=[]))
        loaded = await gom.load(PersonCareer, "p1")
        assert loaded is not None and len(loaded.employment_history) == 1

    async def test_replace_removes_what_the_list_lacks(self, pm, gom):
        await gom.save(
            PersonCareer(
                person=Person(uuid="p1", name="Ada"),
                employment_history=[
                    WorkHistory(start_date="2015", role="Engineer", target=babbage()),
                    WorkHistory(start_date="2020", role="Lead", target=acme()),
                ],
            )
        )
        career = await gom.load(PersonCareer, "p1")
        assert career is not None
        career.employment_history = [it for it in career.employment_history if it.target.uuid == "o1"]
        await gom.save(career, replace=["employment_history"], removed_targets=RemovedTargets.DELETE_UNREFERENCED)
        loaded = await gom.load(PersonCareer, "p1")
        assert loaded is not None and [it.target.name for it in loaded.employment_history] == ["Acme"]
        assert [it["uuid"] for it in await nodes(pm, "Organization")] == ["o1"]

    async def test_replace_all_needs_a_loaded_view(self, gom):
        with pytest.raises(DrivineError, match='replace="all" needs'):
            await gom.save(PersonCareer(person=Person(uuid="p1", name="Ada")), replace="all")

    async def test_a_nested_view_is_saved_level_by_level(self, gom):
        def place(uuid: str, name: str, *below: LocationHierarchy) -> LocationHierarchy:
            return LocationHierarchy(location=Location(uuid=uuid, name=name, type="place"), sub_locations=list(below))

        await gom.save(place("l0", "World", place("l1", "Oceania", place("l2", "Australia"))))
        loaded = await gom.load(LocationHierarchy, "l0")
        assert loaded is not None
        assert loaded.sub_locations[0].sub_locations[0].location.name == "Australia"

    async def test_replaces_a_list_of_views_by_their_roots(self, gom):
        def place(uuid: str, *below: LocationHierarchy) -> LocationHierarchy:
            return LocationHierarchy(location=Location(uuid=uuid, name=uuid, type="place"), sub_locations=list(below))

        await gom.save(place("l0", place("l1"), place("l2")))
        world = await gom.load(LocationHierarchy, "l0")
        assert world is not None
        world.sub_locations = [it for it in world.sub_locations if it.location.uuid == "l2"]
        await gom.save(world, replace=["sub_locations"])
        loaded = await gom.load(LocationHierarchy, "l0")
        assert loaded is not None and [it.location.uuid for it in loaded.sub_locations] == ["l2"]


class TestUpdate:
    async def test_writes_what_the_change_altered(self, pm, gom):
        await gom.save(Person(uuid="p1", name="Ada", bio="Engineer"))
        await set_property(pm, "Person", "extra", "kept")

        def rename(person: Person) -> None:
            person.name = "Ada L"
            person.bio = None

        updated = await gom.update(Person, "p1", rename)
        assert updated is not None and updated.name == "Ada L"
        assert await stored(pm, "Person", "name", "bio", "extra") == ["Ada L", None, "kept"]

    async def test_loads_again_when_another_writer_got_there_first(self, gom):
        await gom.save(Person(uuid="p1", name="Ada", bio="b0"))
        calls = 0

        async def change(person: Person) -> Person:
            nonlocal calls
            calls += 1
            if calls == 1:  # another writer changes the node between this load and save
                other = await gom.load(Person, "p1")
                assert other is not None
                other.bio = "changed meanwhile"
                await gom.save(other)
            return person.model_copy(update={"name": "Ada L"})

        updated = await gom.update(Person, "p1", change)
        assert calls == 2
        assert updated is not None and (updated.name, updated.bio) == ("Ada L", "changed meanwhile")

    async def test_none_for_no_node_and_refuses_another_id(self, gom):
        assert await gom.update(Person, "missing", lambda it: None) is None
        await gom.save(Person(uuid="p1", name="Ada"))
        with pytest.raises(DrivineError, match="another id"):
            await gom.update(Person, "p1", lambda it: it.model_copy(update={"uuid": "p2"}))

    async def test_updates_a_node_whose_packed_text_another_writer_wrote_with_other_spacing_at_the_first_attempt(
        self, pm, gom
    ):
        if pm.type in (DatabaseType.ORACLE, DatabaseType.POSTGRES):
            pytest.skip("the text is written in Cypher")
        await gom.save(Packed(id="k1", text="one", prefs={"a": 1}))
        # As TypeScript's Drivine, or a migration, packs it: not as json.dumps would.
        await pm.execute(QuerySpecification("""MATCH (n:Packed {id: 'k1'}) SET n.prefs = '{"a":1}'"""))

        await gom.update(Packed, "k1", lambda it: setattr(it, "text", "two"), attempts=1)

        updated = await gom.load(Packed, "k1")
        assert updated is not None and (updated.text, updated.prefs) == ("two", {"a": 1})

    async def test_updates_a_node_that_holds_a_packed_field_as_a_list_another_writer_wrote_at_the_first_attempt(
        self, pm, gom
    ):
        if pm.type in (DatabaseType.ORACLE, DatabaseType.POSTGRES):
            pytest.skip("the list is written in Cypher")
        await gom.save(Packed(id="k2", text="one", tags=["x"]))
        # Held as a list, not as text: a load hands it as it is.
        await pm.execute(QuerySpecification("MATCH (n:Packed {id: 'k2'}) SET n.tags = ['x']"))

        await gom.update(Packed, "k2", lambda it: setattr(it, "text", "two"), attempts=1)

        updated = await gom.load(Packed, "k2")
        assert updated is not None and (updated.text, updated.tags) == ("two", ["x"])

    async def test_replaces_a_list_that_lost_an_item(self, gom):
        await gom.save(
            PersonCareer(
                person=Person(uuid="p1", name="Ada"),
                employment_history=[
                    WorkHistory(start_date="2015", role="Engineer", target=babbage()),
                    WorkHistory(start_date="2020", role="Lead", target=acme()),
                ],
            )
        )

        def leave_babbage(career: PersonCareer) -> None:
            career.employment_history = [it for it in career.employment_history if it.target.uuid != "o2"]

        await gom.update(PersonCareer, "p1", leave_babbage)
        loaded = await gom.load(PersonCareer, "p1")
        assert loaded is not None and [it.role for it in loaded.employment_history] == ["Lead"]

    @pytest.mark.async_only
    async def test_updates_of_one_node_by_many_writers_at_once_keep_every_writers_change(self, gom):
        # A load that runs while another writer commits can be handed that writer's stamp with the data from before its
        # write, on an engine that reads without a snapshot. An update of what it loaded then passes the stamp's check
        # and writes over the other writer's change.
        writers, each = 8, 50
        await gom.save(Finding(id="f1"))

        async def writer(w: int) -> None:
            for k in range(each):
                await gom.update(Finding, "f1", lambda it, k=k: it.sources.append(f"w{w}-{k}"), attempts=500)

        await asyncio.wait_for(asyncio.gather(*(writer(w) for w in range(writers))), timeout=300)
        loaded = await gom.load(Finding, "f1")
        assert loaded is not None
        kept = set(loaded.sources)
        assert [it for it in (f"w{w}-{k}" for w in range(writers) for k in range(each)) if it not in kept] == []


async def applied_after_unmarked(
    pm: PersistenceManager,
    gom: StatelessGraphObjectManager,
    type_: type[T],
    id: str,
    unmarked: str,
    change: Callable[[T], Any],
) -> int:
    """
    Runs an update whose first load is overtaken by `unmarked`, Cypher that changes the node and leaves its stamp alone:
    the node as a load sees it when it is handed another writer's stamp with the data from before that writer's write.
    Gives how many times the change was applied.
    """
    applied = 0

    async def overtaken(loaded: T) -> None:
        nonlocal applied
        applied += 1
        if applied == 1:
            await pm.execute(QuerySpecification(unmarked))
        change(loaded)

    assert await gom.update(type_, id, overtaken) is not None
    return applied


@pytest.fixture
def reads_without_snapshot(pm: PersistenceManager) -> None:
    """Neo4j reads without a snapshot: there an update compares the root's fields as loaded, not only the stamp."""
    if pm.type is not DatabaseType.NEO4J:
        pytest.skip("only Neo4j reads without a snapshot")


@pytest.mark.usefixtures("reads_without_snapshot")
class TestUpdateOfATornLoad:
    async def test_loads_again_when_a_field_differs_from_what_it_loaded_under_the_same_stamp(self, pm, gom):
        await gom.save(Finding(id="f1", sources=["first"]))

        unmarked = "MATCH (n:Finding {id: 'f1'}) SET n.sources = n.sources + 'theirs'"
        assert await applied_after_unmarked(pm, gom, Finding, "f1", unmarked, lambda it: it.sources.append("mine")) == 2
        loaded = await gom.load(Finding, "f1")
        assert loaded is not None and sorted(loaded.sources) == ["first", "mine", "theirs"]

    async def test_takes_a_field_loaded_as_its_default_from_a_node_that_holds_none_as_it_was_loaded(self, pm, gom):
        await gom.save(Finding(id="f1"))
        await pm.execute(QuerySpecification("MATCH (n:Finding {id: 'f1'}) REMOVE n.sources"))

        applied = 0

        def mine(it: Finding) -> None:
            nonlocal applied
            applied += 1
            it.sources.append("mine")

        assert await gom.update(Finding, "f1", mine, attempts=2) is not None
        assert applied == 1, "a load gives the absent property its default, so the default is what was loaded"
        loaded = await gom.load(Finding, "f1")
        assert loaded is not None and loaded.sources == ["mine"]

    async def test_takes_fields_a_driver_hands_back_in_its_own_form_as_they_were_loaded(self, pm, gom):
        taken = datetime(2026, 10, 23, 9, 30, 15, 123456, tzinfo=UTC)
        await gom.save(
            Reading(id="r1", taken=taken, on=date(2026, 10, 23), shade=Shade.RED, value=0.1, count=3, pair=("a", "b"))
        )

        def more(it: Reading) -> None:
            it.count += 1

        assert await applied_after_unmarked(pm, gom, Reading, "r1", "RETURN 1", more) == 1
        loaded = await gom.load(Reading, "r1")
        assert loaded is not None and loaded.count == 4 and loaded.taken == taken

    async def test_takes_a_subclass_loaded_as_its_base_as_it_was_loaded(self, pm, gom):
        await gom.save(Hound(id="h1", name="Rex", breed="beagle", labels=["Good"], extras={"size": 1}))

        def renamed(it: Pet) -> None:
            it.name = "Max"

        assert await applied_after_unmarked(pm, gom, Pet, "h1", "RETURN 1", renamed) == 1, (
            "the subclass's own field and label are not the base's bag or labels"
        )
        loaded = await gom.load(Pet, "h1")
        assert isinstance(loaded, Hound) and (loaded.name, loaded.labels, loaded.extras) == (
            "Max",
            ["Good"],
            {"size": 1},
        )

    async def test_loads_again_when_a_bag_entry_differs_from_what_it_loaded_under_the_same_stamp(self, pm, gom):
        await gom.save(Tagged(id="t1", text="one", meta={"owner": "first"}))

        unmarked = "MATCH (n:Tagged {id: 't1'}) SET n.`meta.owner` = 'theirs'"
        assert await applied_after_unmarked(pm, gom, Tagged, "t1", unmarked, lambda it: it.meta.update(mine="yes")) == 2
        loaded = await gom.load(Tagged, "t1")
        assert loaded is not None and loaded.meta == {"owner": "theirs", "mine": "yes"}

    async def test_loads_again_when_a_bag_gained_an_entry_and_not_for_a_property_outside_it(self, pm, gom):
        await gom.save(Tagged(id="t1", text="one", meta={"owner": "first"}))

        def text(value: str) -> Callable[[Tagged], None]:
            return lambda it: setattr(it, "text", value)

        gained = "MATCH (n:Tagged {id: 't1'}) SET n.`meta.extra` = 'theirs'"
        outside = "MATCH (n:Tagged {id: 't1'}) SET n.unrelated = 'theirs'"
        assert await applied_after_unmarked(pm, gom, Tagged, "t1", gained, text("two")) == 2
        assert await applied_after_unmarked(pm, gom, Tagged, "t1", outside, text("three")) == 1
        loaded = await gom.load(Tagged, "t1")
        assert loaded is not None and loaded.meta == {"owner": "first", "extra": "theirs"}

    async def test_loads_again_when_a_flat_bag_or_an_open_set_of_labels_differs(self, pm, gom):
        await gom.save(Sticker(id="s1", labels=["Shiny"], extras={"size": 1}))

        def size(value: int) -> Callable[[Sticker], None]:
            return lambda it: it.extras.update(size=value)

        coloured = "MATCH (n:Sticker {id: 's1'}) SET n.colour = 'red'"
        sneaked = "MATCH (n:Sticker {id: 's1'}) SET n:Sneaked"
        assert await applied_after_unmarked(pm, gom, Sticker, "s1", coloured, size(2)) == 2
        assert await applied_after_unmarked(pm, gom, Sticker, "s1", sneaked, size(3)) == 2
        sticker = await gom.load(Sticker, "s1")
        assert sticker is not None and sticker.extras == {"size": 3, "colour": "red"}
        assert "Sneaked" in sticker.labels

    async def test_loads_again_when_a_closed_set_of_labels_differs_and_not_for_a_label_outside_it(self, pm, gom):
        await gom.save(Swatch(id="w1", shades={Shade.BLUE}))

        red = "MATCH (n:Swatch {id: 'w1'}) SET n:Red"
        unrelated = "MATCH (n:Swatch {id: 'w1'}) SET n:Unrelated"
        assert await applied_after_unmarked(pm, gom, Swatch, "w1", red, lambda it: it.shades.add(Shade.GREEN)) == 2
        assert (
            await applied_after_unmarked(pm, gom, Swatch, "w1", unrelated, lambda it: it.shades.discard(Shade.BLUE))
            == 1
        )
        swatch = await gom.load(Swatch, "w1")
        assert swatch is not None and swatch.shades == {Shade.RED, Shade.GREEN}, (
            "the label another writer added is kept"
        )


class TestSaveFromTheObjectAsLoaded:
    @pytest.mark.usefixtures("reads_without_snapshot")
    async def test_is_refused_when_a_field_changed_under_the_same_stamp_and_a_save_without_it_is_not(self, pm, gom):
        await gom.save(Finding(id="f1", sources=["first"]))
        loaded = await gom.load(Finding, "f1")
        assert loaded is not None
        # As a load sees the node when it is handed another writer's stamp with the data from before its write.
        await pm.execute(QuerySpecification("MATCH (n:Finding {id: 'f1'}) SET n.sources = n.sources + 'theirs'"))

        with pytest.raises(StaleObjectError, match="a field is not as it was loaded"):
            await gom.save(loaded.model_copy(update={"sources": [*loaded.sources, "mine"]}), from_=loaded)
        assert sorted(await stored(pm, "Finding", "sources")) == ["first", "theirs"], "the other writer's change stands"

        # The stamp alone can't tell: a save without from_ writes over the other writer's change.
        await gom.save(loaded.model_copy(update={"sources": [*loaded.sources, "mine"]}))
        assert sorted(await stored(pm, "Finding", "sources")) == ["first", "mine"]

    async def test_is_applied_when_the_node_is_as_loaded_and_hands_back_the_new_stamp(self, gom):
        await gom.save(Finding(id="f1", sources=["first"]))
        loaded = await gom.load(Finding, "f1")
        assert loaded is not None

        saved = await gom.save(loaded.model_copy(update={"sources": [*loaded.sources, "mine"]}), from_=loaded)

        again = await gom.load(Finding, "f1")
        assert again is not None and sorted(again.sources) == ["first", "mine"]
        assert saved.stamp != loaded.stamp, "the save changed the node"
        assert saved.stamp == again.stamp

    async def test_is_checked_against_the_stamp_it_was_loaded_with_whatever_the_copy_carries(self, pm, gom):
        await gom.save(Finding(id="f1", sources=["first"]))
        loaded = await gom.load(Finding, "f1")
        theirs = await gom.load(Finding, "f1")
        assert loaded is not None and theirs is not None
        theirs.sources = ["theirs"]
        await gom.save(theirs)

        # Built anew, without the stamp: it is checked as what it was loaded from.
        with pytest.raises(StaleObjectError):
            await gom.save(Finding(id="f1", sources=["mine"]), from_=loaded)

        assert await stored(pm, "Finding", "sources") == ["theirs"]

    async def test_from_another_object_than_the_one_loaded_is_refused_before_anything_is_written(self, pm, gom):
        await gom.save(Finding(id="f1", sources=["first"]))
        await gom.save(Finding(id="f2", sources=["second"]))
        loaded, other = await gom.load(Finding, "f1"), await gom.load(Finding, "f2")
        assert loaded is not None and other is not None
        mine = loaded.model_copy(update={"sources": ["mine"]})

        with pytest.raises(DrivineError, match="from_ is the Finding 'f2', and the object saved 'f1'"):
            await gom.save(mine, from_=other)
        with pytest.raises(DrivineError, match="carries no stamp"):
            await gom.save(mine, from_=loaded.model_copy(update={"stamp": None}))
        with pytest.raises(DrivineError, match="from_ is a Tagged, and the object saved a Finding"):
            await gom.save(mine, from_=Tagged(id="f1", text="one", stamp=loaded.stamp))  # pyright: ignore
        with pytest.raises(DrivineError, match="from_ is the object saved itself"):
            await gom.save(mine, from_=mine)

        assert sorted(it["sources"][0] for it in await nodes(pm, "Finding")) == ["first", "second"], "nothing written"

    async def test_of_a_view_copied_to_change_a_relationship_alone_is_applied(self, gom):
        await gom.save(PersonCareer(person=Person(uuid="p1", name="Ada")))
        loaded = await gom.load(PersonCareer, "p1")
        assert loaded is not None

        # The copy shares the root, unchanged, with what was loaded.
        joined = [WorkHistory(start_date="2020", role="Lead", target=acme())]
        await gom.save(loaded.model_copy(update={"employment_history": joined}), from_=loaded)

        again = await gom.load(PersonCareer, "p1")
        assert again is not None and [it.role for it in again.employment_history] == ["Lead"]

    @pytest.mark.usefixtures("reads_without_snapshot")
    async def test_of_a_view_copied_to_change_a_relationship_is_refused_when_its_root_changed_under_the_stamp(
        self, pm, gom
    ):
        await gom.save(PersonCareer(person=Person(uuid="p1", name="Ada")))
        loaded = await gom.load(PersonCareer, "p1")
        assert loaded is not None
        await pm.execute(QuerySpecification("MATCH (n:Person {uuid: 'p1'}) SET n.name = 'Theirs'"))

        joined = [WorkHistory(start_date="2020", role="Lead", target=acme())]
        with pytest.raises(StaleObjectError, match="a field is not as it was loaded"):
            await gom.save(loaded.model_copy(update={"employment_history": joined}), from_=loaded)

        assert await stored(pm, "Person", "name") == "Theirs", "the other writer's change stands"
        assert await gom.count(Organization) == 0, "nothing was written"

    @pytest.mark.usefixtures("reads_without_snapshot")
    async def test_compares_a_packed_field_as_a_save_packs_it(self, pm, gom):
        await gom.save(Packed(id="k1", text="one", prefs={"a": 1}))
        loaded = await gom.load(Packed, "k1")
        assert loaded is not None
        saved = await gom.save(loaded.model_copy(update={"text": "two"}), from_=loaded)

        # The same value, packed as TypeScript's Drivine or a migration packs it: from_ holds the value, not the text,
        # so the save can't tell this from a change and is refused.
        await pm.execute(QuerySpecification("""MATCH (n:Packed {id: 'k1'}) SET n.prefs = '{"a":1}'"""))
        with pytest.raises(StaleObjectError, match="a field is not as it was loaded"):
            await gom.save(saved.model_copy(update={"text": "three"}), from_=saved)
        assert await stored(pm, "Packed", "text") == "two"

    @pytest.mark.async_only
    async def test_saves_by_many_writers_at_once_keep_every_writers_change(self, gom):
        writers, each = 8, 25
        await gom.save(Finding(id="f1"))

        async def writer(w: int) -> None:
            for k in range(each):
                # Load, change and save from what was loaded; again when another writer got there first.
                while True:
                    loaded = await gom.load(Finding, "f1")
                    assert loaded is not None
                    try:
                        await gom.save(
                            loaded.model_copy(update={"sources": [*loaded.sources, f"w{w}-{k}"]}), from_=loaded
                        )
                        break
                    except StaleObjectError:
                        pass

        await asyncio.wait_for(asyncio.gather(*(writer(w) for w in range(writers))), timeout=300)
        loaded = await gom.load(Finding, "f1")
        assert loaded is not None
        kept = set(loaded.sources)
        assert [it for it in (f"w{w}-{k}" for w in range(writers) for k in range(each)) if it not in kept] == []


class TestDelete:
    async def test_deletes_by_id_and_all(self, pm, gom):
        for it in range(3):
            await gom.save(Person(uuid=f"p{it}", name=f"n{it}"))
        assert await gom.delete(Person, "p0") == 1
        assert await gom.delete(Person, "p0") == 0
        assert await gom.delete_all(Person, equals(pm, "name", "name"), {"name": "n1"}) == 1
        assert [it.uuid for it in await gom.load_all(Person)] == ["p2"]

    async def test_cascades(self, pm, gom):
        def career(uuid: str, *orgs: Organization) -> PersonCareer:
            history = [WorkHistory(start_date="2020", role="r", target=it) for it in orgs]
            return PersonCareer(person=Person(uuid=uuid, name=uuid), employment_history=history)

        await gom.save(career("p1", acme(), babbage()))
        await gom.save(career("p2", acme()))
        if pm.type.value == "MEMGRAPH":
            with pytest.raises(DrivineError, match="DELETE_ORPHAN is not available on Memgraph"):
                await gom.delete(PersonCareer, "p1", cascade=CascadeType.DELETE_ORPHAN)
            return
        assert await gom.delete(PersonCareer, "p1", cascade=CascadeType.DELETE_ORPHAN) == 1
        assert [it["uuid"] for it in await nodes(pm, "Organization")] == ["o1"]
        assert await gom.delete(PersonCareer, "p2", cascade=CascadeType.DELETE_ALL) == 1
        assert await gom.count(Organization) == 0


class TestConcurrency:
    @pytest.mark.async_only
    async def test_of_two_saves_from_one_load_one_is_refused(self, pm, gom):
        await gom.save(Person(uuid="p1", name="Ada"))
        first, second = await gom.load(Person, "p1"), await gom.load(Person, "p1")
        first.name, second.name = "First", "Second"
        results = await asyncio.gather(gom.save(first), gom.save(second), return_exceptions=True)
        assert sum(isinstance(it, StaleObjectError) for it in results) == 1
        winner = next(it for it in results if not isinstance(it, BaseException))
        assert await stored(pm, "Person", "name") == winner.name

    @pytest.mark.rolls_back
    async def test_a_save_joins_the_transaction(self, pm, gom):
        with pytest.raises(ValueError):
            async with transaction():
                await gom.save(Person(uuid="p1", name="Ada"))
                raise ValueError("roll back")
        assert await gom.count(Person) == 0
