# Written by scripts/unasync.py from tests/integration/test_object_manager_saving.py:
# change that, and run the script again.
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Any, TypeVar

import pytest

from drivine.sync import (
    CascadeType,
    DatabaseType,
    DrivineError,
    JsonPacked,
    NodeFragment,
    NodeId,
    NodeLabels,
    NodeStamp,
    NullPolicy,
    PatchResult,
    PersistenceManager,
    PropertyBag,
    QuerySpecification,
    RemovedTargets,
    StaleObjectError,
    StatelessGraphObjectManager,
    cypher_statement,
    node_ref,
    refs,
    stamps,
    transaction,
)
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
from tests.sets import reordered_set
from tests.sync_integration.stores import equals, labels, nodes, set_property

T = TypeVar("T")


class Finding(NodeFragment, labels=["Finding"]):
    id: Annotated[str, NodeId()]
    sources: list[str] = []
    stamp: Annotated[str | None, NodeStamp()] = None


class Note(NodeFragment, labels=["Note"]):
    """A fragment with no NodeStamp field: no save of it is checked."""

    id: Annotated[str, NodeId()]
    text: str | None = None


class Loose(NodeFragment, labels=["Loose"]):
    """A fragment with no NodeId field: no save of it can merge on an id."""

    text: str | None = None


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


class Bag(NodeFragment, labels=["Bag"]):
    """A set field, which the store holds as a list in an order, and maybe with a duplicate, the set doesn't keep."""

    id: Annotated[str, NodeId()]
    name: str | None = None
    tags: set[str] = set()
    stamp: Annotated[str | None, NodeStamp()] = None


class PackedBag(NodeFragment, labels=["PackedBag"]):
    """A set field packed as JSON text, which a check compares as the text a save packs."""

    id: Annotated[str, NodeId()]
    name: str | None = None
    tags: Annotated[set[str], JsonPacked()] = set()
    stamp: Annotated[str | None, NodeStamp()] = None


@pytest.fixture
def gom(pm: PersistenceManager) -> StatelessGraphObjectManager:
    return StatelessGraphObjectManager(pm)


def stored(pm: PersistenceManager, label: str, *props: str) -> object:
    """A property of the one node with the label, or a list of several, as the store holds them."""
    [node] = nodes(pm, label)
    return node.get(props[0]) if len(props) == 1 else [node.get(it) for it in props]


def acme() -> Organization:
    return Organization(uuid="o1", name="Acme")


def babbage() -> Organization:
    return Organization(uuid="o2", name="Babbage & Co")


class TestFragments:
    def test_saves_and_hands_back_a_whole_stamp(self, gom):
        ada = gom.save(Person(uuid="p1", name="Ada", bio="Engineer"))
        assert ada.stamp is not None and len(ada.stamp) == 2 * stamps.TOKEN + 1
        assert gom.load(Person, "p1") == ada

    def test_refuses_a_stale_save_and_writes_nothing(self, pm, gom):
        gom.save(Person(uuid="p1", name="Ada"))
        first = gom.load(Person, "p1")
        second = gom.load(Person, "p1")
        assert first is not None and second is not None
        second.name = "Ada L"
        gom.save(second)
        first.name = "Countess"
        with pytest.raises(StaleObjectError) as raised:
            gom.save(first)
        assert not raised.value.deleted and raised.value.found_stamp == second.stamp
        assert stored(pm, "Person", "name") == "Ada L"

    def test_a_save_of_a_deleted_node_is_stale(self, gom):
        gom.save(Person(uuid="p1", name="Ada"))
        loaded = gom.load(Person, "p1")
        gom.delete(Person, "p1")
        with pytest.raises(StaleObjectError) as raised:
            gom.save(loaded)
        assert raised.value.deleted

    def test_null_policies(self, pm, gom):
        gom.save(Person(uuid="p1", name="Ada", bio="Engineer"))
        gom.save(Person(uuid="p1", name="Ada"))  # IGNORE: bio left as it is
        assert stored(pm, "Person", "bio") == "Engineer"
        gom.save(Person(uuid="p1", name="Ada"), null_policy=NullPolicy.CLEAR)  # bio never assigned: left
        assert stored(pm, "Person", "bio") == "Engineer"
        gom.save(Person(uuid="p1", name="Ada", bio=None), null_policy=NullPolicy.CLEAR)  # assigned None: cleared
        assert stored(pm, "Person", "bio") is None

    def test_only_and_except(self, pm, gom):
        gom.save(Person(uuid="p1", name="Ada", bio="Engineer"))
        gom.save(Person(uuid="p1", name="Grace", bio="Admiral"), only=["bio"])
        assert stored(pm, "Person", "name", "bio") == ["Ada", "Admiral"]
        gom.save(Person(uuid="p1", name="Grace", bio="Rear admiral"), except_=["bio"])
        assert stored(pm, "Person", "name", "bio") == ["Grace", "Admiral"]
        with pytest.raises(DrivineError, match="not both"):
            gom.save(Person(uuid="p1", name="x"), only=["name"], except_=["bio"])

    def test_bags_labels_and_packed_values_round_trip(self, gom):
        customer = Customer(
            uuid="c1",
            full_name="Ada L",
            tags=["vip"],
            preferences={"theme": "dark", "sizes": [1, 2]},
            metadata={"source": "wiki", "score": 3},
            tiers={"Gold"},
            legacy_code="X-1",
        )
        gom.save(customer)
        loaded = gom.load(Customer, "c1")
        assert loaded is not None
        assert loaded.model_dump(exclude={"extras"}) == customer.model_dump(exclude={"extras"})

    @pytest.mark.parametrize("key", ["fullName", "uuid", "LEGACY", "metadata.source", "__drivine.stamp"])
    def test_refuses_a_flat_bag_entry_a_field_another_bag_or_the_stamp_is_stored_under(self, pm, gom, key):
        customer = Customer(uuid="c1", full_name="Ada L", extras={key: "x"})
        with pytest.raises(DrivineError, match=f"Customer.extras has an entry '{key}', which is the property of a"):
            gom.save(customer)
        with pytest.raises(DrivineError, match="which is the property of a declared field"):
            gom.save_all([customer])
        with pytest.raises(DrivineError, match="which is the property of a declared field"):
            gom.save(customer, only=["extras"])
        assert nodes(pm, "Customer") == []

    def test_a_flat_bag_takes_an_entry_named_like_a_field_stored_under_another_name(self, gom):
        gom.save(Customer(uuid="c1", full_name="Ada L", extras={"full_name": "open", "size": 3}))
        loaded = gom.load(Customer, "c1")
        assert loaded is not None
        assert (loaded.full_name, loaded.extras) == ("Ada L", {"full_name": "open", "size": 3})

    def test_a_flat_bag_stores_a_key_that_isnt_a_string_under_its_text(self, gom):
        gom.save(Customer.model_construct(uuid="c1", full_name="Ada L", extras={1: "x"}))
        loaded = gom.load(Customer, "c1")
        assert loaded is not None
        assert loaded.extras == {"1": "x"}

    def test_a_subclass_is_written_with_its_own_labels(self, pm, gom):
        gom.save(Puppy(uuid="a1", name="Bit", age_in_weeks=9))
        assert labels(pm, "a1") == ["Animal", "Dog", "Puppy"]
        assert type(gom.load(Animal, "a1")) is Puppy

    def test_create_makes_only_what_is_absent(self, pm, gom):
        assert gom.create(Person(uuid="p1", name="Ada")) is not None
        assert gom.create(Person(uuid="p1", name="Imposter")) is None
        assert stored(pm, "Person", "name") == "Ada"
        loaded = gom.load(Person, "p1")
        with pytest.raises(DrivineError, match="carries a stamp was loaded"):
            gom.create(loaded)


class TestViews:
    def test_adds_relationships_and_related_nodes(self, pm, gom):
        career = PersonCareer(
            person=Person(uuid="p1", name="Ada"),
            employment_history=[WorkHistory(start_date="2015", role="Engineer", target=babbage())],
        )
        gom.save(career)
        career.employment_history.append(WorkHistory(start_date="2020", role="Lead", target=acme()))
        saved = gom.save(career)
        loaded = gom.load(PersonCareer, "p1")
        assert loaded is not None
        assert [(it.role, it.target.name) for it in loaded.employment_history] == [
            ("Engineer", "Babbage & Co"),
            ("Lead", "Acme"),
        ]
        assert loaded.person.stamp == saved.person.stamp

    def test_a_save_without_replace_removes_nothing(self, gom):
        gom.save(
            PersonCareer(
                person=Person(uuid="p1", name="Ada"),
                employment_history=[WorkHistory(start_date="2015", role="Engineer", target=babbage())],
            )
        )
        gom.save(PersonCareer(person=Person(uuid="p1", name="Ada"), employment_history=[]))
        loaded = gom.load(PersonCareer, "p1")
        assert loaded is not None and len(loaded.employment_history) == 1

    def test_replace_removes_what_the_list_lacks(self, pm, gom):
        gom.save(
            PersonCareer(
                person=Person(uuid="p1", name="Ada"),
                employment_history=[
                    WorkHistory(start_date="2015", role="Engineer", target=babbage()),
                    WorkHistory(start_date="2020", role="Lead", target=acme()),
                ],
            )
        )
        career = gom.load(PersonCareer, "p1")
        assert career is not None
        career.employment_history = [it for it in career.employment_history if it.target.uuid == "o1"]
        gom.save(career, replace=["employment_history"], removed_targets=RemovedTargets.DELETE_UNREFERENCED)
        loaded = gom.load(PersonCareer, "p1")
        assert loaded is not None and [it.target.name for it in loaded.employment_history] == ["Acme"]
        assert [it["uuid"] for it in nodes(pm, "Organization")] == ["o1"]

    def test_replace_all_needs_a_loaded_view(self, gom):
        with pytest.raises(DrivineError, match='replace="all" needs'):
            gom.save(PersonCareer(person=Person(uuid="p1", name="Ada")), replace="all")

    def test_a_nested_view_is_saved_level_by_level(self, gom):
        def place(uuid: str, name: str, *below: LocationHierarchy) -> LocationHierarchy:
            return LocationHierarchy(location=Location(uuid=uuid, name=name, type="place"), sub_locations=list(below))

        gom.save(place("l0", "World", place("l1", "Oceania", place("l2", "Australia"))))
        loaded = gom.load(LocationHierarchy, "l0")
        assert loaded is not None
        assert loaded.sub_locations[0].sub_locations[0].location.name == "Australia"

    def test_replaces_a_list_of_views_by_their_roots(self, gom):
        def place(uuid: str, *below: LocationHierarchy) -> LocationHierarchy:
            return LocationHierarchy(location=Location(uuid=uuid, name=uuid, type="place"), sub_locations=list(below))

        gom.save(place("l0", place("l1"), place("l2")))
        world = gom.load(LocationHierarchy, "l0")
        assert world is not None
        world.sub_locations = [it for it in world.sub_locations if it.location.uuid == "l2"]
        gom.save(world, replace=["sub_locations"])
        loaded = gom.load(LocationHierarchy, "l0")
        assert loaded is not None and [it.location.uuid for it in loaded.sub_locations] == ["l2"]


class TestUpdate:
    def test_writes_what_the_change_altered(self, pm, gom):
        gom.save(Person(uuid="p1", name="Ada", bio="Engineer"))
        set_property(pm, "Person", "extra", "kept")

        def rename(person: Person) -> None:
            person.name = "Ada L"
            person.bio = None

        updated = gom.update(Person, "p1", rename)
        assert updated is not None and updated.name == "Ada L"
        assert stored(pm, "Person", "name", "bio", "extra") == ["Ada L", None, "kept"]

    def test_loads_again_when_another_writer_got_there_first(self, gom):
        gom.save(Person(uuid="p1", name="Ada", bio="b0"))
        calls = 0

        def change(person: Person) -> Person:
            nonlocal calls
            calls += 1
            if calls == 1:  # another writer changes the node between this load and save
                other = gom.load(Person, "p1")
                assert other is not None
                other.bio = "changed meanwhile"
                gom.save(other)
            return person.model_copy(update={"name": "Ada L"})

        updated = gom.update(Person, "p1", change)
        assert calls == 2
        assert updated is not None and (updated.name, updated.bio) == ("Ada L", "changed meanwhile")

    def test_none_for_no_node_and_refuses_another_id(self, gom):
        assert gom.update(Person, "missing", lambda it: None) is None
        gom.save(Person(uuid="p1", name="Ada"))
        with pytest.raises(DrivineError, match="another id"):
            gom.update(Person, "p1", lambda it: it.model_copy(update={"uuid": "p2"}))

    def test_updates_a_node_whose_packed_text_another_writer_wrote_with_other_spacing_at_the_first_attempt(
        self, pm, gom
    ):
        gom.save(Packed(id="k1", text="one", prefs={"a": 1}))
        # As TypeScript's Drivine, or a migration, packs it: not as json.dumps would.
        packed = """MATCH (n:Packed {id: 'k1'}) SET n.prefs = '{"a":1}'"""
        pm.execute(QuerySpecification(cypher_statement(packed)))

        gom.update(Packed, "k1", lambda it: setattr(it, "text", "two"), attempts=1)

        updated = gom.load(Packed, "k1")
        assert updated is not None and (updated.text, updated.prefs) == ("two", {"a": 1})

    def test_updates_a_node_that_holds_a_packed_field_as_a_list_another_writer_wrote_at_the_first_attempt(
        self, pm, gom
    ):
        gom.save(Packed(id="k2", text="one", tags=["x"]))
        # Held as a list, not as text: a load hands it as it is.
        pm.execute(QuerySpecification(cypher_statement("MATCH (n:Packed {id: 'k2'}) SET n.tags = ['x']")))

        gom.update(Packed, "k2", lambda it: setattr(it, "text", "two"), attempts=1)

        updated = gom.load(Packed, "k2")
        assert updated is not None and (updated.text, updated.tags) == ("two", ["x"])

    def test_replaces_a_list_that_lost_an_item(self, gom):
        gom.save(
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

        gom.update(PersonCareer, "p1", leave_babbage)
        loaded = gom.load(PersonCareer, "p1")
        assert loaded is not None and [it.role for it in loaded.employment_history] == ["Lead"]


def applied_after_unmarked(
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

    def overtaken(loaded: T) -> None:
        nonlocal applied
        applied += 1
        if applied == 1:
            pm.execute(QuerySpecification(unmarked))
        change(loaded)

    assert gom.update(type_, id, overtaken) is not None
    return applied


@pytest.fixture
def reads_without_snapshot(pm: PersistenceManager) -> None:
    """Neo4j reads without a snapshot: there an update compares the root's fields as loaded, not only the stamp."""
    if pm.type is not DatabaseType.NEO4J:
        pytest.skip("only Neo4j reads without a snapshot")


@pytest.mark.usefixtures("reads_without_snapshot")
class TestUpdateOfATornLoad:
    def test_loads_again_when_a_field_differs_from_what_it_loaded_under_the_same_stamp(self, pm, gom):
        gom.save(Finding(id="f1", sources=["first"]))

        unmarked = "MATCH (n:Finding {id: 'f1'}) SET n.sources = n.sources + 'theirs'"
        assert applied_after_unmarked(pm, gom, Finding, "f1", unmarked, lambda it: it.sources.append("mine")) == 2
        loaded = gom.load(Finding, "f1")
        assert loaded is not None and sorted(loaded.sources) == ["first", "mine", "theirs"]

    def test_takes_a_field_loaded_as_its_default_from_a_node_that_holds_none_as_it_was_loaded(self, pm, gom):
        gom.save(Finding(id="f1"))
        pm.execute(QuerySpecification("MATCH (n:Finding {id: 'f1'}) REMOVE n.sources"))

        applied = 0

        def mine(it: Finding) -> None:
            nonlocal applied
            applied += 1
            it.sources.append("mine")

        assert gom.update(Finding, "f1", mine, attempts=2) is not None
        assert applied == 1, "a load gives the absent property its default, so the default is what was loaded"
        loaded = gom.load(Finding, "f1")
        assert loaded is not None and loaded.sources == ["mine"]

    def test_takes_fields_a_driver_hands_back_in_its_own_form_as_they_were_loaded(self, pm, gom):
        taken = datetime(2026, 10, 23, 9, 30, 15, 123456, tzinfo=UTC)
        gom.save(
            Reading(id="r1", taken=taken, on=date(2026, 10, 23), shade=Shade.RED, value=0.1, count=3, pair=("a", "b"))
        )

        def more(it: Reading) -> None:
            it.count += 1

        assert applied_after_unmarked(pm, gom, Reading, "r1", "RETURN 1", more) == 1
        loaded = gom.load(Reading, "r1")
        assert loaded is not None and loaded.count == 4 and loaded.taken == taken

    def test_takes_a_subclass_loaded_as_its_base_as_it_was_loaded(self, pm, gom):
        gom.save(Hound(id="h1", name="Rex", breed="beagle", labels=["Good"], extras={"size": 1}))

        def renamed(it: Pet) -> None:
            it.name = "Max"

        assert applied_after_unmarked(pm, gom, Pet, "h1", "RETURN 1", renamed) == 1, (
            "the subclass's own field and label are not the base's bag or labels"
        )
        loaded = gom.load(Pet, "h1")
        assert isinstance(loaded, Hound) and (loaded.name, loaded.labels, loaded.extras) == (
            "Max",
            ["Good"],
            {"size": 1},
        )

    def test_loads_again_when_a_bag_entry_differs_from_what_it_loaded_under_the_same_stamp(self, pm, gom):
        gom.save(Tagged(id="t1", text="one", meta={"owner": "first"}))

        unmarked = "MATCH (n:Tagged {id: 't1'}) SET n.`meta.owner` = 'theirs'"
        assert applied_after_unmarked(pm, gom, Tagged, "t1", unmarked, lambda it: it.meta.update(mine="yes")) == 2
        loaded = gom.load(Tagged, "t1")
        assert loaded is not None and loaded.meta == {"owner": "theirs", "mine": "yes"}

    def test_loads_again_when_a_bag_gained_an_entry_and_not_for_a_property_outside_it(self, pm, gom):
        gom.save(Tagged(id="t1", text="one", meta={"owner": "first"}))

        def text(value: str) -> Callable[[Tagged], None]:
            return lambda it: setattr(it, "text", value)

        gained = "MATCH (n:Tagged {id: 't1'}) SET n.`meta.extra` = 'theirs'"
        outside = "MATCH (n:Tagged {id: 't1'}) SET n.unrelated = 'theirs'"
        assert applied_after_unmarked(pm, gom, Tagged, "t1", gained, text("two")) == 2
        assert applied_after_unmarked(pm, gom, Tagged, "t1", outside, text("three")) == 1
        loaded = gom.load(Tagged, "t1")
        assert loaded is not None and loaded.meta == {"owner": "first", "extra": "theirs"}

    def test_loads_again_when_a_flat_bag_or_an_open_set_of_labels_differs(self, pm, gom):
        gom.save(Sticker(id="s1", labels=["Shiny"], extras={"size": 1}))

        def size(value: int) -> Callable[[Sticker], None]:
            return lambda it: it.extras.update(size=value)

        coloured = "MATCH (n:Sticker {id: 's1'}) SET n.colour = 'red'"
        sneaked = "MATCH (n:Sticker {id: 's1'}) SET n:Sneaked"
        assert applied_after_unmarked(pm, gom, Sticker, "s1", coloured, size(2)) == 2
        assert applied_after_unmarked(pm, gom, Sticker, "s1", sneaked, size(3)) == 2
        sticker = gom.load(Sticker, "s1")
        assert sticker is not None and sticker.extras == {"size": 3, "colour": "red"}
        assert "Sneaked" in sticker.labels

    def test_loads_again_when_a_closed_set_of_labels_differs_and_not_for_a_label_outside_it(self, pm, gom):
        gom.save(Swatch(id="w1", shades={Shade.BLUE}))

        red = "MATCH (n:Swatch {id: 'w1'}) SET n:Red"
        unrelated = "MATCH (n:Swatch {id: 'w1'}) SET n:Unrelated"
        assert applied_after_unmarked(pm, gom, Swatch, "w1", red, lambda it: it.shades.add(Shade.GREEN)) == 2
        assert applied_after_unmarked(pm, gom, Swatch, "w1", unrelated, lambda it: it.shades.discard(Shade.BLUE)) == 1
        swatch = gom.load(Swatch, "w1")
        assert swatch is not None and swatch.shades == {Shade.RED, Shade.GREEN}, (
            "the label another writer added is kept"
        )


class TestSaveFromTheObjectAsLoaded:
    @pytest.mark.usefixtures("reads_without_snapshot")
    def test_is_refused_when_a_field_changed_under_the_same_stamp_and_a_save_without_it_is_not(self, pm, gom):
        gom.save(Finding(id="f1", sources=["first"]))
        loaded = gom.load(Finding, "f1")
        assert loaded is not None
        # As a load sees the node when it is handed another writer's stamp with the data from before its write.
        pm.execute(QuerySpecification("MATCH (n:Finding {id: 'f1'}) SET n.sources = n.sources + 'theirs'"))

        with pytest.raises(StaleObjectError, match="a field is not as it was loaded"):
            gom.save(loaded.model_copy(update={"sources": [*loaded.sources, "mine"]}), from_=loaded)
        assert sorted(stored(pm, "Finding", "sources")) == ["first", "theirs"], "the other writer's change stands"

        # The stamp alone can't tell: a save without from_ writes over the other writer's change.
        gom.save(loaded.model_copy(update={"sources": [*loaded.sources, "mine"]}))
        assert sorted(stored(pm, "Finding", "sources")) == ["first", "mine"]

    def test_is_applied_when_the_node_is_as_loaded_and_hands_back_the_new_stamp(self, gom):
        gom.save(Finding(id="f1", sources=["first"]))
        loaded = gom.load(Finding, "f1")
        assert loaded is not None

        saved = gom.save(loaded.model_copy(update={"sources": [*loaded.sources, "mine"]}), from_=loaded)

        again = gom.load(Finding, "f1")
        assert again is not None and sorted(again.sources) == ["first", "mine"]
        assert saved.stamp != loaded.stamp, "the save changed the node"
        assert saved.stamp == again.stamp

    def test_is_checked_against_the_stamp_it_was_loaded_with_whatever_the_copy_carries(self, pm, gom):
        gom.save(Finding(id="f1", sources=["first"]))
        loaded = gom.load(Finding, "f1")
        theirs = gom.load(Finding, "f1")
        assert loaded is not None and theirs is not None
        theirs.sources = ["theirs"]
        gom.save(theirs)

        # Built anew, without the stamp: it is checked as what it was loaded from.
        with pytest.raises(StaleObjectError):
            gom.save(Finding(id="f1", sources=["mine"]), from_=loaded)

        assert stored(pm, "Finding", "sources") == ["theirs"]

    def test_from_another_object_than_the_one_loaded_is_refused_before_anything_is_written(self, pm, gom):
        gom.save(Finding(id="f1", sources=["first"]))
        gom.save(Finding(id="f2", sources=["second"]))
        loaded, other = gom.load(Finding, "f1"), gom.load(Finding, "f2")
        assert loaded is not None and other is not None
        mine = loaded.model_copy(update={"sources": ["mine"]})

        with pytest.raises(DrivineError, match="from_ is the Finding 'f2', and the object saved 'f1'"):
            gom.save(mine, from_=other)
        with pytest.raises(DrivineError, match="carries no stamp"):
            gom.save(mine, from_=loaded.model_copy(update={"stamp": None}))
        with pytest.raises(DrivineError, match="from_ is a Tagged, and the object saved a Finding"):
            gom.save(mine, from_=Tagged(id="f1", text="one", stamp=loaded.stamp))  # pyright: ignore
        with pytest.raises(DrivineError, match="from_ is the object saved itself"):
            gom.save(mine, from_=mine)
        note = Note(id="n1", text="one")
        with pytest.raises(DrivineError, match="Note has no NodeStamp field"):
            gom.save(note.model_copy(update={"text": "two"}), from_=note)
        assert nodes(pm, "Note") == [], "nothing written"
        loose = Loose(text="one")
        with pytest.raises(DrivineError, match="Loose has no NodeId field"):
            gom.save(loose.model_copy(update={"text": "two"}), from_=loose)
        assert nodes(pm, "Loose") == [], "nothing written"

        assert sorted(it["sources"][0] for it in nodes(pm, "Finding")) == ["first", "second"], "nothing written"

    def test_a_view_without_its_root_on_either_side_is_refused_before_anything_is_written(self, pm, gom):
        gom.save(PersonCareer(person=Person(uuid="p1", name="Ada")))
        loaded = gom.load(PersonCareer, "p1")
        assert loaded is not None
        rootless = loaded.model_copy(update={"person": None})
        renamed = loaded.model_copy(update={"person": loaded.person.model_copy(update={"name": "Grace"})})

        with pytest.raises(DrivineError, match="The PersonCareer saved has no root fragment"):
            gom.save(rootless, from_=loaded)
        with pytest.raises(DrivineError, match="The PersonCareer given as from_ has no root fragment"):
            gom.save(renamed, from_=rootless)

        assert stored(pm, "Person", "name") == "Ada", "nothing written"

    def test_of_a_view_copied_to_change_a_relationship_alone_is_applied(self, gom):
        gom.save(PersonCareer(person=Person(uuid="p1", name="Ada")))
        loaded = gom.load(PersonCareer, "p1")
        assert loaded is not None

        # The copy shares the root, unchanged, with what was loaded.
        joined = [WorkHistory(start_date="2020", role="Lead", target=acme())]
        gom.save(loaded.model_copy(update={"employment_history": joined}), from_=loaded)

        again = gom.load(PersonCareer, "p1")
        assert again is not None and [it.role for it in again.employment_history] == ["Lead"]

    @pytest.mark.usefixtures("reads_without_snapshot")
    def test_of_a_view_copied_to_change_a_relationship_is_refused_when_its_root_changed_under_the_stamp(self, pm, gom):
        gom.save(PersonCareer(person=Person(uuid="p1", name="Ada")))
        loaded = gom.load(PersonCareer, "p1")
        assert loaded is not None
        pm.execute(QuerySpecification("MATCH (n:Person {uuid: 'p1'}) SET n.name = 'Theirs'"))

        joined = [WorkHistory(start_date="2020", role="Lead", target=acme())]
        with pytest.raises(StaleObjectError, match="a field is not as it was loaded"):
            gom.save(loaded.model_copy(update={"employment_history": joined}), from_=loaded)

        assert stored(pm, "Person", "name") == "Theirs", "the other writer's change stands"
        assert gom.count(Organization) == 0, "nothing was written"

    @pytest.mark.usefixtures("reads_without_snapshot")
    def test_compares_a_packed_field_as_a_save_packs_it(self, pm, gom):
        gom.save(Packed(id="k1", text="one", prefs={"a": 1}))
        loaded = gom.load(Packed, "k1")
        assert loaded is not None
        saved = gom.save(loaded.model_copy(update={"text": "two"}), from_=loaded)

        # The same value, packed as TypeScript's Drivine or a migration packs it: from_ holds the value, not the text,
        # so the save can't tell this from a change and is refused.
        pm.execute(QuerySpecification("""MATCH (n:Packed {id: 'k1'}) SET n.prefs = '{"a":1}'"""))
        with pytest.raises(StaleObjectError, match="a field is not as it was loaded"):
            gom.save(saved.model_copy(update={"text": "three"}), from_=saved)
        assert stored(pm, "Packed", "text") == "two"


class TestDelete:
    def test_deletes_by_id_and_all(self, pm, gom):
        for it in range(3):
            gom.save(Person(uuid=f"p{it}", name=f"n{it}"))
        assert gom.delete(Person, "p0") == 1
        assert gom.delete(Person, "p0") == 0
        assert gom.delete_all(Person, equals(pm, "name", "name"), {"name": "n1"}) == 1
        assert [it.uuid for it in gom.load_all(Person)] == ["p2"]

    def test_cascades(self, pm, gom):
        def career(uuid: str, *orgs: Organization) -> PersonCareer:
            history = [WorkHistory(start_date="2020", role="r", target=it) for it in orgs]
            return PersonCareer(person=Person(uuid=uuid, name=uuid), employment_history=history)

        gom.save(career("p1", acme(), babbage()))
        gom.save(career("p2", acme()))
        if pm.type.value == "MEMGRAPH":
            with pytest.raises(DrivineError, match="DELETE_ORPHAN is not available on Memgraph"):
                gom.delete(PersonCareer, "p1", cascade=CascadeType.DELETE_ORPHAN)
            return
        assert gom.delete(PersonCareer, "p1", cascade=CascadeType.DELETE_ORPHAN) == 1
        assert [it["uuid"] for it in nodes(pm, "Organization")] == ["o1"]
        assert gom.delete(PersonCareer, "p2", cascade=CascadeType.DELETE_ALL) == 1
        assert gom.count(Organization) == 0


class TestConcurrency:
    @pytest.mark.rolls_back
    def test_a_save_joins_the_transaction(self, pm, gom):
        with pytest.raises(ValueError):
            with transaction():
                gom.save(Person(uuid="p1", name="Ada"))
                raise ValueError("roll back")
        assert gom.count(Person) == 0


FIVE = ["zeta", "alpha", "mid", "beta", "omega"]


@pytest.mark.usefixtures("reads_without_snapshot")
class TestASetFieldAsLoaded:
    """
    A set is stored as a list, and Neo4j compares lists item by item. The set loaded from the list may iterate in
    another order and holds no duplicate, so the check compares it with the stored list as a set.
    """

    @staticmethod
    def held_as(pm: PersistenceManager, tags: list[str]) -> None:
        """Stores the list as the bag's tags, leaving its stamp alone: as another library or plain Cypher writes it."""
        pm.execute(QuerySpecification("MATCH (n:Bag {id: 'b1'}) SET n.tags = $tags").bind({"tags": tags}))

    @staticmethod
    def renamed(it: Bag) -> None:
        it.name = "two"

    def test_a_stored_duplicate_saves_from_the_loaded_copy_and_in_an_update(self, pm, gom):
        gom.save(Bag(id="b1", name="one", tags={"x"}))
        self.held_as(pm, ["a", "a", "b"])

        loaded = gom.load(Bag, "b1")
        assert loaded is not None and loaded.tags == {"a", "b"}
        assert gom.update(Bag, "b1", self.renamed, attempts=1) is not None
        assert stored(pm, "Bag", "name", "tags") == ["two", ["a", "a", "b"]], "an update wrote the name alone"

        loaded = gom.load(Bag, "b1")
        assert loaded is not None
        gom.save(loaded.model_copy(update={"name": "three"}), from_=loaded)
        assert stored(pm, "Bag", "name", "tags") == ["three", ["a", "b"]], "a save writes the whole set"

    def test_a_stored_order_the_set_doesnt_iterate_in_saves_from_the_loaded_copy_and_in_an_update(self, pm, gom):
        gom.save(Bag(id="b1", name="one", tags=set(FIVE)))
        loaded = gom.load(Bag, "b1")
        assert loaded is not None
        # The node holds the set's items in an order other than the one the loaded set iterates in.
        other = list(reversed(list(loaded.tags)))
        self.held_as(pm, other)

        assert gom.update(Bag, "b1", self.renamed, attempts=1) is not None
        assert stored(pm, "Bag", "tags") == other, "an update wrote the name alone"

        loaded = gom.load(Bag, "b1")
        assert loaded is not None
        self.held_as(pm, other[1:] + other[:1])
        gom.save(loaded.model_copy(update={"name": "three"}), from_=loaded)
        assert stored(pm, "Bag", "name", "tags") == ["three", sorted(FIVE)], "a save writes the set sorted"

    def test_a_set_drivine_saved_saves_again_from_its_loaded_copy_and_from_what_the_save_handed_back(self, pm, gom):
        tags = reordered_set("tag")
        gom.save(Bag(id="b1", name="one", tags=tags))
        loaded = gom.load(Bag, "b1")
        assert loaded is not None

        saved = gom.save(loaded.model_copy(update={"name": "two"}), from_=loaded)
        gom.save(saved.model_copy(update={"name": "three"}), from_=saved)
        assert gom.update(Bag, "b1", lambda it: setattr(it, "name", "four"), attempts=1) is not None

        again = gom.load(Bag, "b1")
        assert again is not None and (again.name, again.tags) == ("four", tags)

    def test_a_changed_set_is_written_from_the_loaded_copy_and_in_an_update(self, pm, gom):
        gom.save(Bag(id="b1", name="one", tags={"x"}))
        self.held_as(pm, [*FIVE, "alpha"])
        loaded = gom.load(Bag, "b1")
        assert loaded is not None

        gom.save(loaded.model_copy(update={"tags": {*loaded.tags, "new"}}), from_=loaded)
        assert sorted(stored(pm, "Bag", "tags")) == sorted([*FIVE, "new"])
        assert gom.update(Bag, "b1", lambda it: it.tags.discard("mid"), attempts=1) is not None

        again = gom.load(Bag, "b1")
        assert again is not None and again.tags == {*FIVE, "new"} - {"mid"}

    def test_a_set_another_writer_changed_under_the_same_stamp_is_still_refused(self, pm, gom):
        gom.save(Bag(id="b1", name="one", tags=set(FIVE)))
        loaded = gom.load(Bag, "b1")
        assert loaded is not None
        self.held_as(pm, [*FIVE, "theirs"])

        with pytest.raises(StaleObjectError, match="a field is not as it was loaded"):
            gom.save(loaded.model_copy(update={"name": "two"}), from_=loaded)
        with pytest.raises(StaleObjectError, match="a field is not as it was loaded"):
            gom.save(loaded.model_copy(update={"name": "two"}), from_=loaded.model_copy(update={"tags": {"x"}}))
        pm.execute(QuerySpecification("MATCH (n:Bag {id: 'b1'}) SET n.tags = 'zeta'"))
        with pytest.raises(StaleObjectError, match="a field is not as it was loaded"):
            gom.save(loaded.model_copy(update={"name": "two"}), from_=loaded.model_copy(update={"tags": {"zeta"}}))
        assert stored(pm, "Bag", "name") == "one", "nothing written"
        self.held_as(pm, [*FIVE, "theirs"])

        unmarked = "MATCH (n:Bag {id: 'b1'}) SET n.tags = n.tags + 'later'"
        assert applied_after_unmarked(pm, gom, Bag, "b1", unmarked, self.renamed) == 2
        assert sorted(stored(pm, "Bag", "tags")) == sorted([*FIVE, "theirs", "later"])


LONG_TEXTS = ["x" * 40_000, "é" * 20_000]
"""Texts of 40,000 bytes, past the 32,767 a VARCHAR2 holds on Oracle: one character a byte, and two."""


def bios(gom: StatelessGraphObjectManager) -> dict[str, str | None]:
    """Each Person's bio, by uuid, as loaded."""
    return {it.uuid: it.bio for it in gom.load_all(Person)}


@pytest.mark.parametrize("text", LONG_TEXTS, ids=["ascii", "multibyte"])
class TestLongText:
    def test_saves_loads_and_saves_again_a_node_with_a_long_property(self, gom, text):
        ada = gom.save(Person(uuid="p1", name="Ada", bio=text))
        assert gom.load(Person, "p1") == ada
        ada.name = "Ada L"
        saved = gom.save(ada)  # found, checked against its stamp and merged
        loaded = gom.load(Person, "p1")
        assert loaded == saved and loaded is not None and loaded.bio == text

    def test_a_cypher_statement_writes_a_long_parameter(self, pm, gom, text):
        # A Cypher statement's parameter goes as its JSON text, which past 32,767 bytes is bound as a CLOB.
        gom.save(Person(uuid="p1", name="Ada", bio="short"))
        pm.execute(
            QuerySpecification(cypher_statement("MATCH (n:Person {uuid: 'p1'}) SET n.bio = $text")).bind({"text": text})
        )
        loaded = gom.load(Person, "p1")
        assert loaded is not None and loaded.bio == text

    def test_save_all_writes_nodes_with_long_properties(self, gom, text):
        gom.save_all([Person(uuid=f"p{it}", name=f"n{it}", bio=f"{it}{text}") for it in range(3)])
        assert bios(gom) == {f"p{it}": f"{it}{text}" for it in range(3)}

    def test_patch_patch_all_and_update_grow_a_property_past_the_limit(self, gom, text):
        gom.save_all([Person(uuid="p1", name="Ada", bio="short"), Person(uuid="p2", name="Grace")])
        assert gom.patch(Person, "p1", set={"bio": text}) == PatchResult(True, True, True)
        results = gom.patch_all([Person(uuid="p2", name="Grace", bio=f"2{text}")], only=["bio"])
        assert results == [PatchResult(True, True, True)]

        def grow(person: Person) -> None:
            person.bio = f"{person.bio}{text}"

        gom.update(Person, "p1", grow)
        assert bios(gom) == {"p1": f"{text}{text}", "p2": f"2{text}"}

    def test_a_view_writes_long_properties_on_related_nodes_and_relationships(self, gom, text):
        career = PersonCareer(
            person=Person(uuid="p1", name="Ada", bio=text),
            employment_history=[
                WorkHistory(start_date="2015", role=text, target=Organization(uuid="o1", name=f"Acme {text}"))
            ],
        )
        gom.save(career)
        loaded = gom.load(PersonCareer, "p1")
        assert loaded is not None and loaded.person.bio == text
        assert [(it.role, it.target.name) for it in loaded.employment_history] == [(text, f"Acme {text}")]

    def test_a_node_with_an_open_set_of_labels_keeps_a_long_property(self, gom, text):
        gom.save(Sticker(id="s1", labels=["Shiny"], extras={"note": text}))
        loaded = gom.load(Sticker, "s1")
        assert loaded is not None and loaded.labels == ["Shiny"] and loaded.extras == {"note": text}

    def test_relates_with_a_long_property(self, gom, text):
        gom.save_all([Person(uuid="p1", name="Ada"), Organization(uuid="o1", name="Acme")])
        ada, acme = node_ref(Person, "p1"), node_ref(Organization, "o1")
        assert gom.edges.relate(ada, acme, "WORKS_FOR", {"startDate": "2020", "role": text})
        assert gom.edges.relate(ada, acme, "WORKS_FOR", {"startDate": "2020", "role": text})  # merged
        career = gom.load(PersonCareer, "p1")
        assert career is not None and [it.role for it in career.employment_history] == [text]

    def test_a_view_writes_a_long_property_on_a_relationship_alone(self, gom, text):
        career = PersonCareer(
            person=Person(uuid="p1", name="Ada"),
            employment_history=[WorkHistory(start_date="2015", role=text, target=acme())],
        )
        gom.save(career)
        career.employment_history[0].role = f"2{text}"
        gom.save(career)  # the relationship there, its properties merged
        loaded = gom.load(PersonCareer, "p1")
        assert loaded is not None and [it.role for it in loaded.employment_history] == [f"2{text}"]


def test_patch_and_patch_all_write_a_property_past_4000_bytes(gom):
    # JSON_SERIALIZE with no RETURNING is VARCHAR2(4000) on Oracle (ORA-40478), short of a VARCHAR2's 32,767 bytes.
    text = "x" * 5_000
    gom.save_all([Person(uuid="p1", name="Ada"), Person(uuid="p2", name="Grace")])
    assert gom.patch(Person, "p1", set={"bio": text}) == PatchResult(True, True, True)
    assert gom.patch_all([Person(uuid="p2", name="Grace", bio=text)], only=["bio"]) == [PatchResult(True, True, True)]
    assert bios(gom) == {"p1": text, "p2": text}


def test_a_packed_set_drivine_saved_saves_again_from_its_loaded_copy(pm, gom):
    tags = reordered_set("tag")
    gom.save(PackedBag(id="b1", name="one", tags=tags))
    loaded = gom.load(PackedBag, "b1")
    assert loaded is not None

    saved = gom.save(loaded.model_copy(update={"name": "two"}), from_=loaded)
    gom.save(saved.model_copy(update={"name": "three"}), from_=saved)
    assert stored(pm, "PackedBag", "tags") == json.dumps(sorted(tags)), "a packed set is written sorted"

    assert (gom.patch(PackedBag, "b1", set={"tags": tags})).applied
    assert stored(pm, "PackedBag", "tags") == json.dumps(sorted(tags)), "and patched sorted"
    loaded = gom.load(PackedBag, "b1")
    assert loaded is not None
    gom.save(loaded.model_copy(update={"name": "four"}), from_=loaded)


def test_a_where_finds_a_packed_set_by_a_set_that_iterates_in_another_order(pm, gom):
    tags = reordered_set("tag")
    gom.save(PackedBag(id="b1", name="one", tags=tags))
    other = set(list(tags))
    assert other == tags and list(other) != list(tags)

    q = refs(PackedBag)
    assert [it.id for it in gom.load_all(PackedBag, where=q.tags.eq(other))] == ["b1"]
    assert [it.id for it in gom.load_all(PackedBag, where=q.tags.in_([{"x"}, other]))] == ["b1"]
    assert gom.load_all(PackedBag, where=q.tags.neq(other)) == []

    # Packed before sets were packed sorted, in the order the set iterated in: it loads, but a where packs the set
    # sorted, so it finds the node only once a save has written the set again.
    assert list(tags) != sorted(tags)
    set_property(pm, "PackedBag", "tags", json.dumps(list(tags)))
    loaded = gom.load(PackedBag, "b1")
    assert loaded is not None and loaded.tags == tags
    assert gom.load_all(PackedBag, where=q.tags.eq(other)) == []
    gom.save(loaded)
    assert [it.id for it in gom.load_all(PackedBag, where=q.tags.eq(other))] == ["b1"]


def test_a_patched_set_is_written_sorted_as_a_save_writes_it_and_a_where_finds_it(pm, gom):
    tags = reordered_set("tag")
    gom.save(Bag(id="b1", name="one", tags={"x"}))

    assert (gom.patch(Bag, "b1", set={"tags": tags})).applied
    assert stored(pm, "Bag", "tags") == sorted(tags), "patched as a save writes it"
    if pm.type in (DatabaseType.NEO4J, DatabaseType.MEMGRAPH, DatabaseType.FALKORDB):
        assert [it.id for it in gom.load_all(Bag, where=refs(Bag).tags.eq(set(list(tags))))] == ["b1"]
