# Written by scripts/unasync.py from tests/integration/test_object_manager_saving.py:
# change that, and run the script again.
from collections.abc import Callable
from enum import StrEnum
from typing import Annotated, Any, TypeVar

import pytest

from drivine.sync import (
    CascadeType,
    DatabaseType,
    DrivineError,
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
from tests.sync_integration.stores import equals, labels, nodes, set_property

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
