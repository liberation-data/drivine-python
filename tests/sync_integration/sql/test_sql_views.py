# Written by scripts/unasync.py from tests/integration/sql/test_sql_views.py:
# change that, and run the script again.
from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Any

import pytest

from drivine.sync import (
    Aggregate,
    AggregateFunction,
    Count,
    Direction,
    GraphPath,
    GraphProperty,
    GraphView,
    NodeFragment,
    NodeId,
    PersistenceManager,
    PersistenceManagerFactory,
    Relationship,
    RelationshipFragment,
    Root,
    StatelessGraphObjectManager,
    TargetNode,
    hop,
)
from tests.sync_integration.sql.sql_seed import SqlSeed, raw_property


class Person(NodeFragment, labels=["Person"]):
    id: Annotated[str, NodeId()]
    name: str


class Engineer(Person, labels=["Engineer"]):
    pass


class Org(NodeFragment, labels=["Org"]):
    id: Annotated[str, NodeId()]
    name: str


class City(NodeFragment, labels=["City"]):
    id: Annotated[str, NodeId()]
    name: str


class WorkHistory(RelationshipFragment):
    role: str
    start_year: Annotated[int, GraphProperty("start_year")]
    org: Annotated[Org, TargetNode()]


class Career(GraphView):
    person: Annotated[Person, Root()]
    jobs: Annotated[list[WorkHistory], Relationship("WORKS_FOR")] = []
    city: Annotated[City, Relationship("LIVES_IN", required=True)]
    friends: Annotated[list[Person], Relationship("KNOWS", Direction.UNDIRECTED)] = []
    reports: Annotated[list[Person], Relationship("MANAGES", max_depth=2)] = []
    office_cities: Annotated[list[City], GraphPath([hop("WORKS_FOR"), hop("BASED_IN")])] = []
    ratings: Annotated[int, Count("RATED")] = 0
    avg_score: Annotated[float | None, Aggregate(AggregateFunction.AVG, "RATED", "score")] = None
    total_score: Annotated[float, Aggregate(AggregateFunction.SUM, "RATED", "score")] = 0


class Team(GraphView):
    org: Annotated[Org, Root()]
    members: Annotated[list[Career], Relationship("WORKS_FOR", Direction.INCOMING)] = []


def funded(key: str, ascending: bool = True) -> GraphPath:
    """The cities of the studios a person funded, in the order of the studios' `key`."""
    return GraphPath([hop("FUNDED", Direction.OUTGOING, "Studio", key, ascending), hop("LOCATED_IN")])


class Patron(GraphView):
    """The cities of the studios a person funded, in the order the studios were founded or of their codes."""

    person: Annotated[Person, Root()]
    cities: Annotated[list[City], funded("founded")] = []
    first: Annotated[City | None, funded("founded")] = None
    latest: Annotated[City | None, funded("founded", False)] = None
    latest_first: Annotated[list[City], funded("founded", False)] = []
    by_code: Annotated[list[City], funded("code")] = []
    by_code_descending: Annotated[list[City], funded("code", False)] = []


class RankedPatron(GraphView):
    """The cities of the studios a person funded, by a key that is a text on some studios and a number on others."""

    person: Annotated[Person, Root()]
    cities: Annotated[list[City], funded("sort key")] = []
    cities_descending: Annotated[list[City], funded("sort key", False)] = []


class PatronFriend(GraphView):
    """The patrons a person knows: a path ordered by a hop, in a view nested in another."""

    person: Annotated[Person, Root()]
    knows: Annotated[list[Patron], Relationship("KNOWS")] = []


class CategoryNode(NodeFragment, labels=["Category"]):
    id: Annotated[str, NodeId()]


class Category(GraphView):
    node: Annotated[CategoryNode, Root()]
    parent: Annotated[Category | None, Relationship("PARENT", max_depth=2)] = None


@pytest.fixture(scope="module")
def seeded(factory: PersistenceManagerFactory, store: str) -> Iterator[None]:
    seed = SqlSeed(factory.get(store), "SqlView")
    seed.prepare()
    seed.clear()
    for labels, props in [
        (["Person"], {"id": "ava", "name": "Ada"}),
        (["Person", "Engineer"], {"id": "ben", "name": "Bob"}),
        (["Person"], {"id": "cal", "name": "Cy"}),
        (["Person"], {"id": "dot", "name": "Dee"}),
        (["Org"], {"id": "initech", "name": "Acme"}),
        (["Org"], {"id": "hooli", "name": "Globex"}),
        (["City"], {"id": "paris", "name": "Paris"}),
        (["City"], {"id": "oslo", "name": "Oslo"}),
        (["Movie"], {"id": "m1", "score": 4}),
        (["Movie"], {"id": "m2", "score": 2}),
        (["Movie"], {"id": "m3"}),
        (["Category"], {"id": "c1"}),
        (["Category"], {"id": "c2"}),
        (["Category"], {"id": "c3"}),
        (["Category"], {"id": "c4"}),
        # 999 is first as a number and last as text. '0100' is first as text and last as a number. A studio without
        # founded or code comes last either way.
        (["City"], {"id": "rome", "name": "Rome"}),
        (["Studio"], {"id": "ufa", "founded": 999, "code": "99"}),
        (["Studio"], {"id": "pixar", "founded": 1986, "code": "0100"}),
        (["Studio"], {"id": "bare"}),
        (["City"], {"id": "lyon", "name": "Lyon"}),
        (["City"], {"id": "tokyo", "name": "Tokyo"}),
    ]:
        seed.node(labels, props)
    edges: list[tuple[str, str, str, dict[str, Any] | None]] = [
        ("ava", "WORKS_FOR", "initech", {"role": "lead", "start_year": 2020}),
        ("ava", "WORKS_FOR", "hooli", {"role": "advisor", "start_year": 2024}),
        ("ben", "WORKS_FOR", "initech", {"role": "dev", "start_year": 2022}),
        ("initech", "BASED_IN", "paris", None),
        ("hooli", "BASED_IN", "oslo", None),
        ("ava", "LIVES_IN", "paris", None),
        ("ben", "LIVES_IN", "oslo", None),
        ("ava", "KNOWS", "ben", None),
        ("cal", "KNOWS", "ava", None),
        ("ava", "MANAGES", "ben", None),
        ("ben", "MANAGES", "cal", None),
        ("cal", "MANAGES", "dot", None),
        ("ava", "RATED", "m1", None),
        ("ava", "RATED", "m2", None),
        ("ava", "RATED", "m3", None),
        ("c1", "PARENT", "c2", None),
        ("c2", "PARENT", "c3", None),
        ("c3", "PARENT", "c4", None),
        ("ava", "FUNDED", "bare", None),
        ("ava", "FUNDED", "pixar", None),
        ("ava", "FUNDED", "ufa", None),
        ("bare", "LOCATED_IN", "rome", None),
        ("pixar", "LOCATED_IN", "paris", None),
        ("ufa", "LOCATED_IN", "oslo", None),
    ]
    for from_, type_, to, props in edges:
        seed.edge(from_, type_, to, props)
    # Dee funded studios whose 'sort key' is a text or a number: lyon 'a', paris 'b', oslo 'c' and 5, tokyo 10, and
    # rome none. Lowest first, oslo keeps 'c'; highest first, 5.
    keys: list[tuple[str, str | int | None, str]] = [
        ("gaumont", "a", "lyon"),
        ("pathe", "b", "paris"),
        ("cines", "c", "oslo"),
        ("nordisk", 5, "oslo"),
        ("toho", 10, "tokyo"),
        ("lux", None, "rome"),
    ]
    for studio, key, city in keys:
        seed.node(["Studio"], {"id": studio} if key is None else {"id": studio, "sort key": key})
        seed.edge("dot", "FUNDED", studio)
        seed.edge(studio, "LOCATED_IN", city)
    # Cy funded studios whose 'sort key' is a text, one of them "": oslo '' and 'c', lyon 'a', paris 'b', tokyo 'd'.
    # "" is a text, and the lowest one, so lowest first oslo leads; highest first, oslo keeps 'c'.
    for studio, key, city in [
        ("empty", "", "oslo"),
        ("cee", "c", "oslo"),
        ("ay", "a", "lyon"),
        ("bee", "b", "paris"),
        ("dee", "d", "tokyo"),
    ]:
        seed.node(["Studio"], {"id": studio, "sort key": key})
        seed.edge("cal", "FUNDED", studio)
        seed.edge(studio, "LOCATED_IN", city)
    yield
    seed.clear()


pytestmark = pytest.mark.usefixtures("seeded")


def test_loads_relationship_fragments_single_and_undirected_relationships(gom: StatelessGraphObjectManager):
    ada = gom.load(Career, "ava")
    assert type(ada) is Career
    assert ada.person == Person(id="ava", name="Ada")
    assert ada.city.name == "Paris"
    assert sorted((it.role, it.start_year, it.org.name) for it in ada.jobs) == [
        ("advisor", 2024, "Globex"),
        ("lead", 2020, "Acme"),
    ]
    assert type(ada.jobs[0]) is WorkHistory
    assert sorted(it.id for it in ada.friends) == ["ben", "cal"]
    assert type(next(it for it in ada.friends if it.id == "ben")) is Engineer


def test_reads_deep_fragment_lists_once_each_paths_counts_and_aggregates(gom: StatelessGraphObjectManager):
    ada = gom.load(Career, "ava")
    assert sorted(it.id for it in ada.reports) == ["ben", "cal"]
    assert sorted(it.name for it in ada.office_cities) == ["Oslo", "Paris"]
    assert [ada.ratings, ada.avg_score, ada.total_score] == [3, 3, 6]
    deeper = gom.load(Career, "ava", depth={"reports": 3})
    assert sorted(it.id for it in deeper.reports) == ["ben", "cal", "dot"]
    bob = gom.load(Career, "ben")
    assert [bob.ratings, bob.avg_score, bob.total_score] == [0, None, 0]
    assert [it.id for it in bob.friends] == ["ava"]


def test_reads_a_path_in_the_order_one_of_its_hops_names(gom: StatelessGraphObjectManager):
    ada = gom.load(Patron, "ava")
    assert [it.name for it in ada.cities] == ["Oslo", "Paris", "Rome"]
    assert ada.first.name == "Oslo"
    assert ada.latest.name == "Paris"
    assert [it.name for it in ada.latest_first] == ["Paris", "Oslo", "Rome"]
    bob = gom.load(Patron, "ben")
    assert bob.cities == []
    assert bob.first is None


def test_reads_a_text_of_digits_as_text_and_a_node_without_the_property_last(gom: StatelessGraphObjectManager):
    ada = gom.load(Patron, "ava")
    assert [it.name for it in ada.by_code] == ["Paris", "Oslo", "Rome"]
    assert [it.name for it in ada.by_code_descending] == ["Oslo", "Paris", "Rome"]


def test_reads_a_text_before_a_number_lowest_first_and_after_highest_first(gom: StatelessGraphObjectManager):
    dee = gom.load(RankedPatron, "dot")
    assert [it.name for it in dee.cities] == ["Lyon", "Paris", "Oslo", "Tokyo", "Rome"]
    assert [it.name for it in dee.cities_descending] == ["Tokyo", "Oslo", "Paris", "Lyon", "Rome"]


def test_reads_an_empty_text_as_a_text_and_the_lowest_one(gom: StatelessGraphObjectManager):
    cy = gom.load(RankedPatron, "cal")
    assert [it.name for it in cy.cities] == ["Oslo", "Lyon", "Paris", "Tokyo"]
    assert [it.name for it in cy.cities_descending] == ["Tokyo", "Oslo", "Paris", "Lyon"]


def test_reads_an_ordered_path_of_a_view_nested_in_another(gom: StatelessGraphObjectManager):
    cy = gom.load(PatronFriend, "cal")
    assert [it.person.id for it in cy.knows] == ["ava"]
    assert [it.name for it in cy.knows[0].cities] == ["Oslo", "Paris", "Rome"]
    assert cy.knows[0].latest.name == "Paris"


def test_keeps_only_roots_with_the_required_relationships(gom: StatelessGraphObjectManager):
    assert sorted(it.person.id for it in gom.load_all(Career)) == ["ava", "ben"]
    assert gom.count(Career) == 2
    assert gom.load(Career, "cal") is None


def test_nests_views_and_follows_one_that_holds_itself_to_its_max_depth(gom: StatelessGraphObjectManager):
    acme = gom.load(Team, "initech")
    assert sorted(it.person.id for it in acme.members) == ["ava", "ben"]
    assert next(it for it in acme.members if it.person.id == "ava").city.name == "Paris"
    c1 = gom.load(Category, "c1")
    assert c1.parent.node.id == "c2"
    assert c1.parent.parent.node.id == "c3"
    assert c1.parent.parent.parent is None


def test_filters_views_by_sql_over_the_root_row_and_deletes_a_root_that_meets_them(
    pm: PersistenceManager, gom: StatelessGraphObjectManager
):
    named = gom.load_all(Career, f"{raw_property(pm, 'name')} = $name", {"name": "Bob"})
    assert [it.person.id for it in named] == ["ben"]
    SqlSeed(pm, "SqlView").node(["Person"], {"id": "tmp", "name": "Tmp"})
    assert gom.delete(Career, "tmp") == 0
    assert gom.delete(Person, "tmp") == 1
