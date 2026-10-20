"""
The statements the backend writes for the test models, checked against those TypeScript's Drivine writes for the same
models: tests/golden/statements.json is written by its test/1.Unit/golden/GoldenStatements.spec.ts.
"""

import json
from pathlib import Path

import pytest

from drivine import (
    CascadeType,
    Direction,
    ModelRegistry,
    RelateMode,
    RemovedTargets,
    SaveOptions,
    above,
    any_of,
    at_most,
    case_of,
    coalesce,
    div,
    not_,
    refs,
    times,
)
from drivine.backend.cypher_backend import NODE_ALIAS, CypherBackend
from drivine.backend.cypher_grammar import FALKORDB_GRAMMAR, MEMGRAPH_GRAMMAR, NEO4J_GRAMMAR, CypherGrammar
from drivine.backend.fragment_group import FragmentGroup
from drivine.backend.graph_statement import GraphStatement
from drivine.backend.matched_node import MatchedNode
from drivine.backend.page import Page
from drivine.dsl.query import Query
from drivine.dsl.query_renderer import QueryRenderer
from drivine.model.graph_view_model import GraphViewModel
from tests.models import (
    ActorStats,
    ActorWithLatestMovie,
    ActorWithMovies,
    Animal,
    Customer,
    Dog,
    Location,
    LocationAncestors,
    LocationHierarchy,
    Movie,
    Organization,
    Person,
    PersonCareer,
    Puppy,
    Tier,
    WorkHistory,
)

GOLDEN = json.loads((Path(__file__).parent.parent / "golden" / "statements.json").read_text())


def statements(grammar: CypherGrammar) -> dict[str, str]:
    registry = ModelRegistry()
    backend = CypherBackend(grammar)
    fragment, view = registry.fragment, registry.view
    cases = {
        "Person.loadAll": backend.load_all_fragments(fragment(Person)),
        "Person.loadAll.where": backend.load_all_fragments(fragment(Person), "n.name = $name"),
        "Person.load": backend.load_fragment(fragment(Person)),
        "Person.count": backend.count_fragments(fragment(Person), "n.name = $name"),
        "Puppy.loadAll": backend.load_all_fragments(fragment(Puppy)),
        "PersonCareer.loadAll": backend.load_all_views(view(PersonCareer)),
        "PersonCareer.load": backend.load_view(view(PersonCareer)),
        "PersonCareer.count": backend.count_views(view(PersonCareer), "n.name = $name"),
        "LocationHierarchy.load": backend.load_view(view(LocationHierarchy)),
        "LocationHierarchy.load.depth": backend.load_view(view(LocationHierarchy), {"sub_locations": 5}),
        "LocationAncestors.load": backend.load_view(view(LocationAncestors)),
        "ActorStats.loadAll": backend.load_all_views(view(ActorStats)),
        "ActorWithMovies.loadAll.where": backend.load_all_views(view(ActorWithMovies), "n.name = $name"),
        "ActorWithMovies.count": backend.count_views(view(ActorWithMovies)),
        "ActorWithLatestMovie.loadAll": backend.load_all_views(view(ActorWithLatestMovie)),
    }
    cases.update(saves(grammar, registry, backend))
    cases.update(queries(grammar, registry, backend))
    return {name: it.text for name, it in cases.items()}


def queries(grammar: CypherGrammar, registry: ModelRegistry, backend: CypherBackend) -> dict[str, GraphStatement]:
    """The queries TypeScript's golden test writes, written alike: each load as its statement."""

    def labels(type_: type) -> list[str]:
        return registry.fragment(type_).labels

    def load(target, query: Query) -> GraphStatement:  # noqa: ANN001
        rendered = QueryRenderer.render(query, target, grammar, NODE_ALIAS, labels)
        page = Page(rendered.order_by, rendered.skip, rendered.limit)
        if isinstance(target, GraphViewModel):
            return backend.load_all_views(target, rendered.where, rendered.depth, page)
        return backend.load_all_fragments(target, rendered.where, page)

    m, c, a, p = refs(Movie), refs(PersonCareer), refs(Animal), refs(Customer)
    count_where, _ = QueryRenderer.render_where(
        refs(Person).name.eq_ignore_case("ada"), registry.fragment(Person), grammar, NODE_ALIAS, labels
    )
    return {
        "query.Movie.where": load(
            registry.fragment(Movie),
            Query(
                where=[
                    any_of(m.released.eq(1999), m.title.starts_with("The")),
                    m.uuid.not_in(["m9"]),
                    not_(m.title.eq("x")),
                ],
                order_by=m.released.desc().nulls_last(),
                skip=10,
                limit=5,
            ),
        ),
        "query.Movie.seek": load(
            registry.fragment(Movie),
            Query(
                order_by=[m.released.desc(), m.uuid.asc()], seek=[m.released.after(2000), m.uuid.after("m3")], limit=3
            ),
        ),
        "query.Movie.expression": load(
            registry.fragment(Movie),
            Query(
                order_by=[
                    case_of(
                        [above(m.released, 2010).or_(at_most(m.released, 1995)).then(0)],
                        times(coalesce(m.released, 0), div(1, 2)),
                    ).desc(),
                    m.title.asc().nulls_first(),
                ]
            ),
        ),
        "query.PersonCareer.across": load(
            registry.view(PersonCareer),
            Query(
                where=[
                    c.employment_history.target.name.eq("Acme"),
                    c.person.bio.contains("Lead"),
                    c.employment_history.role.eq("Lead"),
                ]
            ),
        ),
        "query.PersonCareer.quantified": load(
            registry.view(PersonCareer),
            Query(
                where=any_of(
                    c.employment_history.any(lambda it: [it.role.eq("Engineer"), it.target.name.eq("Acme")]),
                    c.employment_history.none(lambda it: it.role.eq("Lead")),
                ),
                order_by=[c.person.name.asc(), c.employment_history.start_date.desc()],
            ),
        ),
        "query.Customer.bags": load(
            registry.fragment(Customer),
            Query(
                where=[
                    p.metadata.source.eq("wiki"),
                    p.property("metadata.score").gte(3),
                    p.predicate("fullName", "STARTS_WITH", "A"),
                    p.tiers.has_item("Gold"),
                    p.tags.has_item("vip"),
                    p.full_name.contains_ignore_case("ad"),
                    p.legacy_code.is_null(),
                ]
            ),
        ),
        "query.Animal.labels": load(
            registry.fragment(Animal), Query(where=any_of(a.instance_of(Puppy), a.has_any_label("Dog", "Cat")))
        ),
        "query.Person.count": backend.count_fragments(registry.fragment(Person), count_where),
    }


STAMP = "aaaaaaaaaaaaaaaa:bbbbbbbbbbbbbbbb"


def saves(grammar: CypherGrammar, registry: ModelRegistry, backend: CypherBackend) -> dict[str, GraphStatement]:
    """The objects TypeScript's golden test builds, built alike: what a statement's text depends on is the same."""

    def save(target, obj, options=None, create_only=False):  # noqa: ANN001, ANN202
        return backend.save(target, obj, options, create_only, registry)

    def career(stamp: str | None = None) -> PersonCareer:
        return PersonCareer(
            person=Person(uuid="p1", name="Ada", stamp=stamp),
            employment_history=[
                WorkHistory(start_date="2015", role="Engineer", target=Organization(uuid="o1", name="Acme")),
                WorkHistory(start_date="2020", role="Engineer", target=Organization(uuid="o2", name="Babbage")),
            ],
        )

    def place(uuid: str, *below: LocationHierarchy) -> LocationHierarchy:
        return LocationHierarchy(location=Location(uuid=uuid, name=uuid, type="place"), sub_locations=list(below))

    fragment, view = registry.fragment, registry.view
    cases = {
        "Person.save": save(fragment(Person), Person(uuid="p1", name="Ada")),
        "Person.save.checked": save(fragment(Person), Person(uuid="p1", name="Ada", stamp=STAMP)),
        "Person.create": save(fragment(Person), Person(uuid="p1", name="Ada"), None, True),
        "Puppy.save": save(fragment(Puppy), Puppy(uuid="a1", name="Bit", age_in_weeks=9)),
        "Customer.save": save(
            fragment(Customer),
            Customer(uuid="c1", full_name="Ada", metadata={"source": "wiki", "score": 3}, tiers={Tier.GOLD}),
        ),
        "PersonCareer.save": save(view(PersonCareer), career()),
        "PersonCareer.save.replace": save(
            view(PersonCareer),
            career(STAMP),
            SaveOptions(replace=["employment_history"], removed_targets=RemovedTargets.DELETE_UNREFERENCED),
        ),
        "LocationHierarchy.save": save(view(LocationHierarchy), place("l0", place("l1", place("l2")))),
        "Person.delete": backend.delete(fragment(Person)),
        "Person.deleteAll": backend.delete_all(fragment(Person), "n.name = $name"),
        "PersonCareer.delete.where": backend.delete(view(PersonCareer), "n.name = $name"),
        "PersonCareer.delete.all": backend.delete(view(PersonCareer), None, CascadeType.DELETE_ALL),
        "LocationHierarchy.delete.all": backend.delete(view(LocationHierarchy), None, CascadeType.DELETE_ALL),
        "Person.heldProperties": backend.held_properties(fragment(Person)),
        "Person.foundStamp": backend.found_stamp(fragment(Person)),
        "saveAll.fragments": backend.fragment_batch(
            [
                FragmentGroup(fragment(Person), [Person(uuid="p1", name="Ada"), Person(uuid="p2", name="Grace")]),
                FragmentGroup(fragment(Organization), [Organization(uuid="o1", name="Acme")]),
            ],
            SaveOptions(),
        ),
        "Person.patch": backend.patch_one(fragment(Person)),
        "Person.patch.where": backend.patch_one(fragment(Person), "n.name = $name"),
        "Person.patchAll": backend.patch_many(fragment(Person)),
        "edges.relate": backend.relate(
            MatchedNode(fragment(Person), "p1"),
            MatchedNode(fragment(Organization), "o1"),
            "WORKS_FOR",
            {"startDate": "2020", "role": "Lead", "note": None},
            RelateMode.MERGE,
        ),
        "edges.relate.create": backend.relate(
            MatchedNode(fragment(Person), "p1"),
            MatchedNode(fragment(Person), "p2", ("Admin",)),
            "KNOWS",
            {},
            RelateMode.CREATE,
        ),
        "edges.unrelate": backend.unrelate(
            MatchedNode(fragment(Person), "p1"), MatchedNode(fragment(Organization), "o1"), "WORKS_FOR"
        ),
        "edges.unrelateAll": backend.unrelate_all(MatchedNode(fragment(Person), "p1"), "KNOWS", Direction.INCOMING),
        "edges.related": backend.related(
            MatchedNode(fragment(Person), "p1"), "OWNS", Direction.OUTGOING, fragment(Dog)
        ),
    }
    if not grammar.orphan_delete_limit:
        cases["PersonCareer.delete.orphans"] = backend.delete(view(PersonCareer), None, CascadeType.DELETE_ORPHAN)
    return cases


@pytest.mark.parametrize(
    "engine, grammar", [("NEO4J", NEO4J_GRAMMAR), ("MEMGRAPH", MEMGRAPH_GRAMMAR), ("FALKORDB", FALKORDB_GRAMMAR)]
)
def test_writes_what_typescript_writes(engine: str, grammar: CypherGrammar):
    written = statements(grammar)
    assert written.keys() == GOLDEN[engine].keys()
    for name, text in written.items():
        assert text == GOLDEN[engine][name], name
