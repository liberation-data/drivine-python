# Written by scripts/unasync.py from tests/integration/test_object_manager_loading.py:
# change that, and run the script again.
import pytest

from drivine.sync import PersistenceManager, QuerySpecification, StatelessGraphObjectManager
from tests.models import (
    ActorStats,
    ActorWithLatestMovie,
    ActorWithMovies,
    Animal,
    AsWritten,
    Customer,
    Dog,
    LocationAncestors,
    LocationHierarchy,
    Organization,
    Person,
    PersonCareer,
    Puppy,
)

# Seeded in Cypher; Oracle's loads are in tests/integration/oracle.
pytestmark = pytest.mark.graph_engines


@pytest.fixture
def gom(pm: PersistenceManager) -> StatelessGraphObjectManager:
    return StatelessGraphObjectManager(pm)


def run(pm: PersistenceManager, statement: str, **bindings: object) -> None:
    pm.execute(QuerySpecification(statement).bind(bindings))


class TestFragments:
    def test_loads_by_id_and_all(self, pm, gom):
        run(
            pm,
            "CREATE (:Person {uuid: 'p1', name: 'Ada', bio: 'Lead engineer', `__drivine.stamp`: 'aaaa:bbbb'}), "
            "(:Person {uuid: 'p2', name: 'Grace'})",
        )
        ada = gom.load(Person, "p1")
        assert ada == Person(uuid="p1", name="Ada", bio="Lead engineer", stamp="aaaa:bbbb")
        assert gom.load(Person, "missing") is None
        everyone = gom.load_all(Person)
        assert sorted(it.name for it in everyone) == ["Ada", "Grace"]

    def test_filters_with_a_predicate_and_counts(self, pm, gom):
        run(pm, "UNWIND range(1, 5) AS i CREATE (:Person {uuid: 'p' + toString(i), name: 'n' + toString(i)})")
        found = gom.load_all(Person, "n.name IN $names", {"names": ["n2", "n4"]})
        assert sorted(it.uuid for it in found) == ["p2", "p4"]
        assert gom.count(Person) == 5
        assert gom.count(Person, "n.name = $name", {"name": "n3"}) == 1

    def test_maps_stored_names_bags_labels_and_defaults(self, pm, gom):
        run(
            pm,
            "CREATE (c:Customer:Gold:Archived {uuid: 'c1', fullName: 'Ada L', status: null, "
            "preferences: '{\"theme\": \"dark\"}', `metadata.source`: 'wiki', `metadata.score`: 3, "
            "LEGACY: 'X-1', nickname: 'al', `__drivine.labels.tiers`: ['Gold']})",
        )
        customer = gom.load(Customer, "c1")
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

    def test_as_written_names(self, pm, gom):
        run(pm, "CREATE (:AsWritten {uuid: 'a1', full_name: 'Ada'})")
        assert gom.load(AsWritten, "a1") == AsWritten(uuid="a1", full_name="Ada")

    def test_loads_the_most_specific_subclass(self, pm, gom):
        run(
            pm,
            "CREATE (:Animal {uuid: 'a1', name: 'Generic'}), (:Animal:Dog {uuid: 'a2', name: 'Rex', breed: 'Kelpie'}), "
            "(:Animal:Dog:Puppy {uuid: 'a3', name: 'Bit', ageInWeeks: 9})",
        )
        animals = {it.uuid: it for it in gom.load_all(Animal)}
        assert type(animals["a1"]) is Animal
        assert type(animals["a2"]) is Dog and animals["a2"].breed == "Kelpie"
        assert type(animals["a3"]) is Puppy and animals["a3"].age_in_weeks == 9
        assert [it.uuid for it in gom.load_all(Dog)] != []
        assert all(isinstance(it, Dog) for it in gom.load_all(Dog))


class TestViews:
    def test_relationship_fragments_sorted(self, pm, gom):
        run(
            pm,
            "CREATE (p:Person {uuid: 'p1', name: 'Ada'}), (a:Organization {uuid: 'o1', name: 'Acme'}), "
            "(b:Organization {uuid: 'o2', name: 'Babbage & Co'}), "
            "(p)-[:WORKS_FOR {startDate: '2020', role: 'Lead'}]->(a), "
            "(p)-[:WORKS_FOR {startDate: '2015', role: 'Engineer'}]->(b), "
            "(:Person {uuid: 'p2', name: 'Grace'})",
        )
        career = gom.load(PersonCareer, "p1")
        assert career is not None
        assert career.person.name == "Ada"
        assert [(it.start_date, it.role, it.target.name) for it in career.employment_history] == [
            ("2015", "Engineer", "Babbage & Co"),
            ("2020", "Lead", "Acme"),
        ]
        assert isinstance(career.employment_history[0].target, Organization)
        careers = gom.load_all(PersonCareer, "n.name = $name", {"name": "Grace"})
        assert len(careers) == 1 and careers[0].employment_history == []

    def test_recursion_to_max_depth_and_its_override(self, pm, gom):
        run(
            pm,
            "CREATE (w:Location {uuid: 'l0', name: 'World', type: 'planet'})"
            "-[:HAS_LOCATION]->(c:Location {uuid: 'l1', name: 'Oceania', type: 'continent'})"
            "-[:HAS_LOCATION]->(n:Location {uuid: 'l2', name: 'Australia', type: 'country'})"
            "-[:HAS_LOCATION]->(s:Location {uuid: 'l3', name: 'NSW', type: 'state'})"
            "-[:HAS_LOCATION]->(:Location {uuid: 'l4', name: 'Sydney', type: 'city'})",
        )

        def depth(view: LocationHierarchy) -> int:
            return 1 + max((depth(it) for it in view.sub_locations), default=0)

        world = gom.load(LocationHierarchy, "l0")
        assert world is not None and depth(world) == 4  # the root and three levels below it
        deeper = gom.load(LocationHierarchy, "l0", depth={"sub_locations": 4})
        assert deeper is not None and depth(deeper) == 5

        sydney = gom.load(LocationAncestors, "l4")
        assert sydney is not None
        assert sorted(it.name for it in sydney.within) == ["Australia", "NSW", "Oceania", "World"]

    def test_paths_aggregates_and_requirements(self, pm, gom):
        run(
            pm,
            "CREATE (a:Actor {uuid: 'a1', name: 'Keanu'}), (b:Actor {uuid: 'a2', name: 'Extra'}), "
            "(m1:Movie {uuid: 'm1', title: 'Late', released: 2003}), (m2:Movie {uuid: 'm2', title: 'Early', "
            "released: 1999}), (d1:Director {uuid: 'd1', name: 'Lana'}), (d2:Director {uuid: 'd2', name: 'Chad'}), "
            "(a)-[:ACTED_IN]->(m1), (a)-[:ACTED_IN]->(m2), (m1)-[:DIRECTED_BY]->(d2), (m2)-[:DIRECTED_BY]->(d1), "
            "(m1)-[:DIRECTED_BY]->(d1), (a)-[:RATED]->(:Rating {score: 4}), (a)-[:RATED]->(:Rating {score: 5}), "
            "(a)-[:RATED]->(:Rating)",  # an aggregate reads the related nodes' property, and passes over a null
        )
        stats = gom.load(ActorStats, "a1")
        assert stats is not None
        assert stats.movie_count == 2
        assert stats.average_rating == 4.5 and stats.best_rating == 5
        assert sorted(it.name for it in stats.directors) == ["Chad", "Lana"]  # each once
        assert stats.first_director is not None and stats.first_director.name == "Lana"  # of the earliest movie
        extra = gom.load(ActorStats, "a2")
        assert extra is not None and (extra.movie_count, extra.average_rating, extra.directors) == (0, None, [])

        assert [it.actor.name for it in gom.load_all(ActorWithMovies)] == ["Keanu"]
        assert gom.count(ActorWithMovies) == 1
        latest = gom.load_all(ActorWithLatestMovie)
        assert [it.actor.name for it in latest] == ["Keanu"]  # a single relationship typed without None is required
        assert latest[0].note == "not loaded"
