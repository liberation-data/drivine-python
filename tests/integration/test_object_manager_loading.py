from typing import Annotated

import pytest

from drivine import (
    EmptyWhenAbsent,
    NodeFragment,
    NodeId,
    PersistenceManager,
    QuerySpecification,
    StatelessGraphObjectManager,
    count,
    key,
    refs,
)
from tests.models import (
    ActorStats,
    ActorWithLatestMovie,
    ActorWithMovies,
    Animal,
    AsWritten,
    Customer,
    Dog,
    Flag,
    LocationAncestors,
    LocationHierarchy,
    Organization,
    Person,
    PersonCareer,
    PlaceAround,
    PlaceReach,
    Puppy,
    StopReach,
)

# Seeded in Cypher, which Postgres and Oracle compile to their SQL.
pytestmark = pytest.mark.written_in_cypher


@pytest.fixture
def gom(pm: PersistenceManager) -> StatelessGraphObjectManager:
    return StatelessGraphObjectManager(pm)


async def run(pm: PersistenceManager, statement: str, **bindings: object) -> None:
    await pm.execute(QuerySpecification(statement).bind(bindings))


@pytest.fixture
async def next_to(pm: PersistenceManager) -> None:
    """p1–p2, s1–s1, s2–s2 and s2–s3, each a NEXT_TO relationship, and two between q1 and q2."""
    await run(
        pm,
        "CREATE (p1:Place {uuid: 'p1'})-[:NEXT_TO]->(:Place {uuid: 'p2'}), "
        "(s1:Place {uuid: 's1'})-[:NEXT_TO]->(s1), "
        "(s2:Place {uuid: 's2'})-[:NEXT_TO]->(s2), (s2)-[:NEXT_TO]->(:Place {uuid: 's3'}), "
        "(q1:Place {uuid: 'q1'})-[:NEXT_TO]->(q2:Place {uuid: 'q2'}), (q1)-[:NEXT_TO]->(q2)",
    )


@pytest.fixture
async def ring(pm: PersistenceManager) -> None:
    """A ring r1 → r2 → r3 → r1 of THEN relationships, and r3 → r4 off it."""
    await run(
        pm,
        "CREATE (r1:Stop {uuid: 'r1'})-[:THEN]->(r2:Stop {uuid: 'r2'})-[:THEN]->(r3:Stop {uuid: 'r3'}), "
        "(r3)-[:THEN]->(r1), (r3)-[:THEN]->(:Stop {uuid: 'r4'})",
    )


class TestFragments:
    async def test_loads_by_id_and_all(self, pm, gom):
        await run(
            pm,
            "CREATE (:Person {uuid: 'p1', name: 'Ada', bio: 'Lead engineer', `__drivine.stamp`: 'aaaa:bbbb'}), "
            "(:Person {uuid: 'p2', name: 'Grace'})",
        )
        ada = await gom.load(Person, "p1")
        assert ada == Person(uuid="p1", name="Ada", bio="Lead engineer", stamp="aaaa:bbbb")
        assert await gom.load(Person, "missing") is None
        everyone = await gom.load_all(Person)
        assert sorted(it.name for it in everyone) == ["Ada", "Grace"]

    async def test_counts(self, pm, gom):
        await run(pm, "UNWIND range(1, 5) AS i CREATE (:Person {uuid: 'p' + toString(i), name: 'n' + toString(i)})")
        assert await gom.count(Person) == 5

    @pytest.mark.graph_engine_only("a raw where is SQL on a SQL store, where `IN` takes no list parameter")
    async def test_filters_with_a_predicate_of_a_list(self, pm, gom):
        await run(pm, "UNWIND range(1, 5) AS i CREATE (:Person {uuid: 'p' + toString(i), name: 'n' + toString(i)})")
        found = await gom.load_all(Person, "n.name IN $names", {"names": ["n2", "n4"]})
        assert sorted(it.uuid for it in found) == ["p2", "p4"]

    async def test_filters_and_counts_with_a_predicate_of_a_property(self, pm, gom):
        await run(pm, "UNWIND range(1, 5) AS i CREATE (:Person {uuid: 'p' + toString(i), name: 'n' + toString(i)})")
        found = await gom.load_all(Person, "n.name = $a OR n.name = $b", {"a": "n2", "b": "n4"})
        assert sorted(it.uuid for it in found) == ["p2", "p4"]
        assert await gom.count(Person, "n.name = $name", {"name": "n3"}) == 1
        careers = await gom.load_all(PersonCareer, "n.name = $name", {"name": "n3"})
        assert len(careers) == 1 and careers[0].employment_history == []

    async def test_reads_a_root_property_written_as_cypher_writes_it_wherever_a_where_is_written(self, pm, gom):
        await run(
            pm,
            "CREATE (:Person {uuid: 'p1', name: 'Crash'}), "
            "(:Person {uuid: 'p2', name: 'Outage'}), (:Person {uuid: 'p3', name: 'n.name'})",
        )
        assert await gom.count(Person, "n.name = 'Crash' OR n.`name` = 'Outage'") == 2
        assert await gom.count(Person, "n.name = 'n.name'") == 1
        patched = await gom.patch(Person, "p2", set={"bio": "b"}, where="n.name = $name", bindings={"name": "Outage"})
        assert patched.applied
        assert await gom.delete(Person, "p1", "n.name = 'y'") == 0
        assert await gom.delete(Person, "p1", "n.name = 'Crash'") == 1
        assert await gom.delete_all(Person, "n.name = 'n.name'") == 1
        rows = await gom.groups.load(
            Person, keys={"name": key("name")}, aggregates={"b": count("n.bio = 'b'")}, where="n.name = 'Outage'"
        )
        assert rows == [{"name": "Outage", "b": 1}]
        await gom.save_all([Flag(uuid="f1", locked=True), Flag(uuid="f2", locked=False)])
        assert [it.uuid for it in await gom.load_all(Flag, "n.locked = true")] == ["f1"]
        assert [it.uuid for it in await gom.load_all(Flag, "n.locked <> TRUE")] == ["f2"]

    async def test_maps_stored_names_bags_labels_and_defaults(self, pm, gom):
        await run(
            pm,
            "CREATE (c:Customer:Gold:Archived {uuid: 'c1', fullName: 'Ada L', status: null, "
            "preferences: '{\"theme\": \"dark\"}', `metadata.source`: 'wiki', `metadata.score`: 3, "
            "LEGACY: 'X-1', nickname: 'al', `__drivine.labels.tiers`: ['Gold']})",
        )
        customer = await gom.load(Customer, "c1")
        assert customer is not None
        assert customer.full_name == "Ada L"
        assert customer.status == "active"  # a stored null loads as the default
        assert customer.tags == []  # absent, loaded empty
        assert customer.preferences == {"theme": "dark"}
        assert customer.metadata == {"source": "wiki", "score": 3}
        assert customer.extras == {"nickname": "al"}  # what no field or other bag is stored as
        assert customer.tiers == {"Gold"}  # a closed set reads only its members
        assert customer.legacy_code == "X-1"
        assert customer.scratch == ""

    async def test_as_written_names(self, pm, gom):
        await run(pm, "CREATE (:AsWritten {uuid: 'a1', full_name: 'Ada'})")
        assert await gom.load(AsWritten, "a1") == AsWritten(uuid="a1", full_name="Ada")

    async def test_loads_the_most_specific_subclass(self, pm, gom):
        await run(
            pm,
            "CREATE (:Animal {uuid: 'a1', name: 'Generic'}), (:Animal:Dog {uuid: 'a2', name: 'Rex', breed: 'Kelpie'}), "
            "(:Animal:Dog:Puppy {uuid: 'a3', name: 'Bit', ageInWeeks: 9})",
        )
        animals = {it.uuid: it for it in await gom.load_all(Animal)}
        assert type(animals["a1"]) is Animal
        assert type(animals["a2"]) is Dog and animals["a2"].breed == "Kelpie"
        assert type(animals["a3"]) is Puppy and animals["a3"].age_in_weeks == 9
        assert [it.uuid for it in await gom.load_all(Dog)] != []
        assert all(isinstance(it, Dog) for it in await gom.load_all(Dog))


class TestViews:
    async def test_relationship_fragments_sorted(self, pm, gom):
        await run(
            pm,
            "CREATE (p:Person {uuid: 'p1', name: 'Ada'}), (a:Organization {uuid: 'o1', name: 'Acme'}), "
            "(b:Organization {uuid: 'o2', name: 'Babbage & Co'}), "
            "(p)-[:WORKS_FOR {startDate: '2020', role: 'Lead'}]->(a), "
            "(p)-[:WORKS_FOR {startDate: '2015', role: 'Engineer'}]->(b), "
            "(:Person {uuid: 'p2', name: 'Grace'})",
        )
        career = await gom.load(PersonCareer, "p1")
        assert career is not None
        assert career.person.name == "Ada"
        assert [(it.start_date, it.role, it.target.name) for it in career.employment_history] == [
            ("2015", "Engineer", "Babbage & Co"),
            ("2020", "Lead", "Acme"),
        ]
        assert isinstance(career.employment_history[0].target, Organization)
        grace = await gom.load(PersonCareer, "p2")
        assert grace is not None and grace.employment_history == []

    async def test_recursion_to_max_depth_and_its_override(self, pm, gom):
        await run(
            pm,
            "CREATE (w:Location {uuid: 'l0', name: 'World', type: 'planet'})"
            "-[:HAS_LOCATION]->(c:Location {uuid: 'l1', name: 'Oceania', type: 'continent'})"
            "-[:HAS_LOCATION]->(n:Location {uuid: 'l2', name: 'Australia', type: 'country'})"
            "-[:HAS_LOCATION]->(s:Location {uuid: 'l3', name: 'NSW', type: 'state'})"
            "-[:HAS_LOCATION]->(:Location {uuid: 'l4', name: 'Sydney', type: 'city'})",
        )

        def depth(view: LocationHierarchy) -> int:
            return 1 + max((depth(it) for it in view.sub_locations), default=0)

        world = await gom.load(LocationHierarchy, "l0")
        assert world is not None and depth(world) == 4  # the root and three levels below it
        deeper = await gom.load(LocationHierarchy, "l0", depth={"sub_locations": 4})
        assert deeper is not None and depth(deeper) == 5

        sydney = await gom.load(LocationAncestors, "l4")
        assert sydney is not None
        assert sorted(it.name for it in sydney.within) == ["Australia", "NSW", "Oceania", "World"]

    async def test_walks_an_undirected_relationship_over_hops_taking_each_once(self, pm, gom, next_to):
        """A walk doesn't go back over the relationship it took, and a self-loop is one relationship (#394)."""
        reaches = {it.place.uuid: sorted(p.uuid for p in it.reach) for it in await gom.load_all(PlaceReach)}
        assert reaches == {
            "p1": ["p2"],
            "p2": ["p1"],
            "s1": ["s1"],
            "s2": ["s2", "s3"],
            "s3": ["s2"],
            # Back over the other of the two relationships: one not yet taken.
            "q1": ["q1", "q2"],
            "q2": ["q1", "q2"],
        }

    async def test_walks_a_list_over_hops_in_a_view_nested_in_another(self, pm, gom, next_to):
        """A nested view's list over hops takes each relationship once in its walk too (#394)."""
        around = {
            it.place.uuid: {near.place.uuid: sorted(p.uuid for p in near.reach) for near in it.around}
            for it in await gom.load_all(PlaceAround)
        }
        assert around == {
            "p1": {"p2": ["p1"]},
            "p2": {"p1": ["p2"]},
            "s1": {"s1": ["s1"]},
            "s2": {"s2": ["s2", "s3"], "s3": ["s2"]},
            "s3": {"s2": ["s2", "s3"]},
            "q1": {"q2": ["q1", "q2"]},
            "q2": {"q1": ["q1", "q2"]},
        }

    async def test_walks_a_directed_relationship_each_way_round_a_ring(self, pm, gom, ring):
        """A directed walk comes back to its root around a ring, and goes no further than its depth (#394)."""
        reaches = {
            it.stop.uuid: (sorted(s.uuid for s in it.ahead), sorted(s.uuid for s in it.behind))
            for it in await gom.load_all(StopReach)
        }
        assert reaches == {
            "r1": (["r1", "r2", "r3", "r4"], ["r2", "r3"]),
            "r2": (["r1", "r2", "r3", "r4"], ["r1", "r3"]),
            "r3": (["r1", "r2", "r3", "r4"], ["r1", "r2"]),
            "r4": ([], ["r2", "r3"]),
        }

    async def test_filters_on_an_undirected_relationship_over_hops_taking_each_once(self, pm, gom, next_to):
        """A query-DSL condition on the list walks it as a load does (#394)."""
        q = refs(PlaceReach)

        async def reaching(uuid: str) -> list[str]:
            return sorted(it.place.uuid for it in await gom.load_all(PlaceReach, where=q.reach.uuid.eq(uuid)))

        assert await reaching("p1") == ["p2"]
        assert await reaching("s3") == ["s2"]
        assert await reaching("s1") == ["s1"]
        assert await reaching("q1") == ["q1", "q2"]

    async def test_paths_aggregates_and_requirements(self, pm, gom):
        await run(
            pm,
            "CREATE (a:Actor {uuid: 'a1', name: 'Keanu'}), (b:Actor {uuid: 'a2', name: 'Extra'}), "
            "(m1:Movie {uuid: 'm1', title: 'Late', released: 2003}), (m2:Movie {uuid: 'm2', title: 'Early', "
            "released: 1999}), (d1:Director {uuid: 'd1', name: 'Lana'}), (d2:Director {uuid: 'd2', name: 'Chad'}), "
            "(a)-[:ACTED_IN]->(m1), (a)-[:ACTED_IN]->(m2), (m1)-[:DIRECTED_BY]->(d2), (m2)-[:DIRECTED_BY]->(d1), "
            "(m1)-[:DIRECTED_BY]->(d1), (a)-[:RATED]->(:Rating {score: 4}), (a)-[:RATED]->(:Rating {score: 5}), "
            "(a)-[:RATED]->(:Rating)",  # an aggregate reads the related nodes' property, and passes over a null
        )
        stats = await gom.load(ActorStats, "a1")
        assert stats is not None
        assert stats.movie_count == 2
        assert stats.average_rating == 4.5 and stats.best_rating == 5
        assert sorted(it.name for it in stats.directors) == ["Chad", "Lana"]  # each once
        assert stats.first_director is not None and stats.first_director.name == "Lana"  # of the earliest movie
        extra = await gom.load(ActorStats, "a2")
        assert extra is not None and (extra.movie_count, extra.average_rating, extra.directors) == (0, None, [])

        assert [it.actor.name for it in await gom.load_all(ActorWithMovies)] == ["Keanu"]
        assert await gom.count(ActorWithMovies) == 1
        latest = await gom.load_all(ActorWithLatestMovie)
        assert [it.actor.name for it in latest] == ["Keanu"]  # a single relationship typed without None is required
        assert latest[0].note == "not loaded"


class CheckedCodes(NodeFragment, labels=["CheckedCodes"]):
    id: Annotated[str, NodeId()]
    codes: set[str] = set()
    pair: tuple[str, ...] = ()
    always_codes: Annotated[set[str], EmptyWhenAbsent()] = set()
    always_pair: Annotated[tuple[str, ...], EmptyWhenAbsent()] = ()


class UncheckedCodes(NodeFragment, labels=["UncheckedCodes"], validate_on_load=False):
    id: Annotated[str, NodeId()]
    codes: set[str] = set()
    pair: tuple[str, ...] = ()
    always_codes: Annotated[set[str], EmptyWhenAbsent()] = set()
    always_pair: Annotated[tuple[str, ...], EmptyWhenAbsent()] = ()


@pytest.mark.parametrize("type_", [CheckedCodes, UncheckedCodes], ids=["validated", "unvalidated"])
class TestCollectionFields:
    """The store holds a set or tuple as a list: it loads as the set or tuple, validated on load or not."""

    async def test_a_saved_set_and_tuple_load_as_a_set_and_a_tuple(self, gom, type_):
        await gom.save(type_(id="1", codes={"x", "y"}, pair=("a", "b"), always_codes={"p"}, always_pair=("c",)))
        await gom.save(type_(id="2"))
        full = await gom.load(type_, "1")
        assert full is not None
        assert type(full.codes) is set and full.codes == {"x", "y"}
        assert type(full.pair) is tuple and full.pair == ("a", "b")
        assert type(full.always_codes) is set and full.always_codes == {"p"}
        assert type(full.always_pair) is tuple and full.always_pair == ("c",)
        empty = await gom.load(type_, "2")
        assert empty is not None
        assert type(empty.codes) is set and empty.codes == set()
        assert type(empty.pair) is tuple and empty.pair == ()
        assert type(empty.always_codes) is set and empty.always_codes == set()
        assert type(empty.always_pair) is tuple and empty.always_pair == ()

    async def test_a_list_written_in_cypher_loads_as_a_set_and_a_tuple(self, pm, gom, type_):
        label = type_.__name__
        await run(pm, f"CREATE (:{label} {{id: '1', codes: ['x', 'y', 'x'], pair: ['a', 'b'], alwaysCodes: ['p']}})")
        loaded = await gom.load(type_, "1")
        assert loaded is not None
        assert type(loaded.codes) is set and loaded.codes == {"x", "y"}
        assert type(loaded.pair) is tuple and loaded.pair == ("a", "b")
        assert type(loaded.always_codes) is set and loaded.always_codes == {"p"}
        assert type(loaded.always_pair) is tuple and loaded.always_pair == ()  # absent, so EmptyWhenAbsent's empty
