import asyncio

import pytest

from drivine import (
    CascadeType,
    DrivineError,
    NullPolicy,
    PersistenceManager,
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


@pytest.fixture
def gom(pm: PersistenceManager) -> StatelessGraphObjectManager:
    return StatelessGraphObjectManager(pm)


async def value(pm: PersistenceManager, statement: str, **bindings: object) -> object:
    return await pm.maybe_get_one(QuerySpecification(statement).bind(bindings))


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
        assert await value(pm, "MATCH (p:Person) RETURN p.name") == "Ada L"

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
        assert await value(pm, "MATCH (p:Person) RETURN p.bio") == "Engineer"
        await gom.save(Person(uuid="p1", name="Ada"), null_policy=NullPolicy.CLEAR)  # bio never assigned: left
        assert await value(pm, "MATCH (p:Person) RETURN p.bio") == "Engineer"
        await gom.save(Person(uuid="p1", name="Ada", bio=None), null_policy=NullPolicy.CLEAR)  # assigned None: cleared
        assert await value(pm, "MATCH (p:Person) RETURN p.bio") is None

    async def test_only_and_except(self, pm, gom):
        await gom.save(Person(uuid="p1", name="Ada", bio="Engineer"))
        await gom.save(Person(uuid="p1", name="Grace", bio="Admiral"), only=["bio"])
        assert await value(pm, "MATCH (p:Person) RETURN [p.name, p.bio]") == ["Ada", "Admiral"]
        await gom.save(Person(uuid="p1", name="Grace", bio="Rear admiral"), except_=["bio"])
        assert await value(pm, "MATCH (p:Person) RETURN [p.name, p.bio]") == ["Grace", "Admiral"]
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

    async def test_a_subclass_is_written_with_its_own_labels(self, pm, gom):
        await gom.save(Puppy(uuid="a1", name="Bit", age_in_weeks=9))
        assert sorted(await value(pm, "MATCH (n {uuid: 'a1'}) RETURN labels(n)")) == ["Animal", "Dog", "Puppy"]
        assert type(await gom.load(Animal, "a1")) is Puppy

    async def test_create_makes_only_what_is_absent(self, pm, gom):
        assert await gom.create(Person(uuid="p1", name="Ada")) is not None
        assert await gom.create(Person(uuid="p1", name="Imposter")) is None
        assert await value(pm, "MATCH (p:Person) RETURN p.name") == "Ada"
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
        assert await value(pm, "MATCH (o:Organization {uuid: 'o2'}) RETURN count(o)") == 0

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
        await pm.execute(QuerySpecification("MATCH (p:Person) SET p.extra = 'kept'"))

        def rename(person: Person) -> None:
            person.name = "Ada L"
            person.bio = None

        updated = await gom.update(Person, "p1", rename)
        assert updated is not None and updated.name == "Ada L"
        assert await value(pm, "MATCH (p:Person) RETURN [p.name, p.bio, p.extra]") == ["Ada L", None, "kept"]

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


class TestDelete:
    async def test_deletes_by_id_and_all(self, gom):
        for it in range(3):
            await gom.save(Person(uuid=f"p{it}", name=f"n{it}"))
        assert await gom.delete(Person, "p0") == 1
        assert await gom.delete(Person, "p0") == 0
        assert await gom.delete_all(Person, "n.name = $name", {"name": "n1"}) == 1
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
        assert sorted(await pm.query(QuerySpecification("MATCH (o:Organization) RETURN o.uuid"))) == ["o1"]
        assert await gom.delete(PersonCareer, "p2", cascade=CascadeType.DELETE_ALL) == 1
        assert await gom.count(Organization) == 0


class TestConcurrency:
    async def test_of_two_saves_from_one_load_one_is_refused(self, pm, gom):
        await gom.save(Person(uuid="p1", name="Ada"))
        first, second = await gom.load(Person, "p1"), await gom.load(Person, "p1")
        first.name, second.name = "First", "Second"
        results = await asyncio.gather(gom.save(first), gom.save(second), return_exceptions=True)
        assert sum(isinstance(it, StaleObjectError) for it in results) == 1
        winner = next(it for it in results if not isinstance(it, BaseException))
        assert await value(pm, "MATCH (p:Person) RETURN p.name") == winner.name

    async def test_a_save_joins_the_transaction(self, pm, gom):
        with pytest.raises(ValueError):
            async with transaction():
                await gom.save(Person(uuid="p1", name="Ada"))
                raise ValueError("roll back")
        assert await gom.count(Person) == 0
