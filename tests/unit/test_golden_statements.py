"""
The statements the backend writes for the test models, checked against those TypeScript's Drivine writes for the same
models: tests/golden/statements.json is written by its test/1.Unit/golden/GoldenStatements.spec.ts.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from drivine import (
    CascadeType,
    Direction,
    EngineVectorOptions,
    ModelRegistry,
    RelateMode,
    RemovedTargets,
    SaveOptions,
    above,
    any_of,
    at_most,
    avg,
    case_of,
    coalesce,
    count,
    div,
    full_text_index,
    hop,
    key,
    max_,
    not_,
    path_key,
    range_index,
    refs,
    sum_,
    times,
    uniqueness_constraint,
    vector_index,
)
from drivine.backend.cypher_backend import CypherBackend
from drivine.backend.cypher_grammar import FALKORDB_GRAMMAR, MEMGRAPH_GRAMMAR, NEO4J_GRAMMAR, CypherGrammar
from drivine.backend.fragment_group import FragmentGroup
from drivine.backend.graph_backend import GraphBackend
from drivine.backend.graph_statement import GraphStatement
from drivine.backend.matched_node import MatchedNode
from drivine.backend.matching_search import MatchingSearch
from drivine.backend.nearest_search import NearestSearch
from drivine.backend.oracle.oracle_backend import OracleBackend
from drivine.backend.oracle.oracle_graph import OracleGraph
from drivine.backend.page import Page
from drivine.connection.database_type import DatabaseType
from drivine.dsl.query import Query
from drivine.manager.group_query import GroupQuery
from drivine.model.graph_view_model import GraphViewModel
from drivine.schema.schema_grammar import (
    FALKORDB_SCHEMA,
    MEMGRAPH_SCHEMA,
    NEO4J_SCHEMA,
    SchemaGrammar,
    schema_grammar_for,
)
from tests.models import (
    ActorStats,
    ActorWithLatestMovie,
    ActorWithMovies,
    Animal,
    Customer,
    Director,
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


CYPHER_WHERE = "n.name = $name"
SQL_WHERE = "JSON_VALUE(n.props, '$.name') = $name"
"""A filter in the store's own language: Cypher over `n`, or SQL over the node row `n` on Oracle."""


def statements(grammar: CypherGrammar) -> dict[str, str]:
    return written(CypherBackend(grammar), CYPHER_WHERE, not grammar.orphan_delete_limit)


def oracle_statements() -> dict[str, str]:
    """Oracle's statements: the same cases, a filter in SQL, and the searches through an index's table too."""
    backend = OracleBackend(OracleGraph())
    person = ModelRegistry().fragment(Person)
    return {
        **written(backend, SQL_WHERE, True),
        "Person.nearest.indexed": backend.nearest(
            person,
            NearestSearch("Person", "embedding", "EUCLIDEAN", 20, limit=5, table="drivine_v_Personembedding_1a2b3c"),
        ).text,
        "Person.matching.indexed": backend.matching(
            person,
            MatchingSearch(
                "Person", ("name", "bio"), 10, text="graph -relational", table="drivine_t_Personnamebio_4d5e6f"
            ),
        ).text,
        "Person.searchIndexTable": backend.search_index_table("VECTOR", "Person", ["embedding"]).text,
    }


def written(backend: GraphBackend, where: str, orphans: bool) -> dict[str, str]:
    registry = ModelRegistry()
    fragment, view = registry.fragment, registry.view
    cases = {
        "Person.loadAll": backend.load_all_fragments(fragment(Person)),
        "Person.loadAll.where": backend.load_all_fragments(fragment(Person), where),
        "Person.load": backend.load_fragment(fragment(Person)),
        "Person.count": backend.count_fragments(fragment(Person), where),
        "Puppy.loadAll": backend.load_all_fragments(fragment(Puppy)),
        "PersonCareer.loadAll": backend.load_all_views(view(PersonCareer)),
        "PersonCareer.load": backend.load_view(view(PersonCareer)),
        "PersonCareer.count": backend.count_views(view(PersonCareer), where),
        "LocationHierarchy.load": backend.load_view(view(LocationHierarchy)),
        "LocationHierarchy.load.depth": backend.load_view(view(LocationHierarchy), {"sub_locations": 5}),
        "LocationAncestors.load": backend.load_view(view(LocationAncestors)),
        "ActorStats.loadAll": backend.load_all_views(view(ActorStats)),
        "ActorWithMovies.loadAll.where": backend.load_all_views(view(ActorWithMovies), where),
        "ActorWithMovies.count": backend.count_views(view(ActorWithMovies)),
        "ActorWithLatestMovie.loadAll": backend.load_all_views(view(ActorWithLatestMovie)),
    }
    cases.update(saves(registry, backend, where, orphans))
    cases.update(queries(registry, backend))
    cases.update(groups(registry, backend))
    cases.update(searches(registry, backend, where))
    return {name: it.text for name, it in cases.items()}


def searches(registry: ModelRegistry, backend: GraphBackend, where: str) -> dict[str, GraphStatement]:
    """The vector and full-text searches TypeScript's golden test writes, written alike."""
    person, career = registry.fragment(Person), registry.view(PersonCareer)
    vector = NearestSearch("Person", "embedding", "COSINE", 5)
    text = MatchingSearch("Person", ("name", "bio"), 10, text="graph database")
    return {
        "Person.nearest": backend.nearest(person, vector),
        "Person.nearest.filtered": backend.nearest(
            person, NearestSearch("Person", "embedding", "EUCLIDEAN", 20, where, True, 5)
        ),
        "PersonCareer.nearest": backend.nearest(career, vector),
        "Person.matching": backend.matching(person, text),
        "Person.matching.filtered": backend.matching(
            person, MatchingSearch("Person", ("bio",), 3, where, True, text="graph database")
        ),
        "PersonCareer.matching": backend.matching(career, text),
    }


def groups(registry: ModelRegistry, backend: GraphBackend) -> dict[str, GraphStatement]:
    """The grouped reads TypeScript's golden test writes, written alike."""
    movie = registry.fragment(Movie)
    directors = {"director": registry.fragment(Director)}

    def labels(type_: type) -> list[str]:
        return registry.fragment(type_).labels

    return {
        "groups.byProperty": backend.groups(
            movie, GroupQuery({"year": key("released")}, {"films": count()}), {}, labels
        ),
        "groups.aggregates": backend.groups(
            movie,
            GroupQuery(
                keys={"year": key("released")},
                aggregates={
                    "films": count(),
                    "nineties": count("n.released < 2000"),
                    "years": sum_("released", "n.released > 2000"),
                    "average": avg("released"),
                    "last": max_("released"),
                },
                where="n.released > $from",
                bindings={"from": 1990},
                having=[("films", "gte", 1)],
                order_by=[("films", "desc")],
                skip=1,
                limit=2,
            ),
            {},
            labels,
        ),
        "groups.byPath": backend.groups(
            movie,
            GroupQuery(
                keys={"year": key("released"), "director": path_key(Director, [hop("DIRECTED_BY")])},
                aggregates={"films": count()},
                order_by=[("year", "desc")],
            ),
            directors,
            labels,
        ),
    }


def queries(registry: ModelRegistry, backend: GraphBackend) -> dict[str, GraphStatement]:
    """The queries TypeScript's golden test writes, written alike: each load as its statement."""

    def labels(type_: type) -> list[str]:
        return registry.fragment(type_).labels

    def load(target, query: Query) -> GraphStatement:  # noqa: ANN001
        rendered = backend.render_query(query, target, labels)
        page = Page(rendered.order_by, rendered.skip, rendered.limit)
        if isinstance(target, GraphViewModel):
            return backend.load_all_views(target, rendered.where, rendered.depth, page)
        return backend.load_all_fragments(target, rendered.where, page)

    m, c, a, p = refs(Movie), refs(PersonCareer), refs(Animal), refs(Customer)
    count_where, _ = backend.render_where(refs(Person).name.eq_ignore_case("ada"), registry.fragment(Person), labels)
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


def saves(registry: ModelRegistry, backend: GraphBackend, where: str, orphans: bool) -> dict[str, GraphStatement]:
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
        "Person.deleteAll": backend.delete_all(fragment(Person), where),
        "PersonCareer.delete.where": backend.delete(view(PersonCareer), where),
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
        "Person.patch.where": backend.patch_one(fragment(Person), where),
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
    if orphans:
        cases["PersonCareer.delete.orphans"] = backend.delete(view(PersonCareer), None, CascadeType.DELETE_ORPHAN)
    return cases


def schema(grammar: SchemaGrammar) -> dict[str, str]:
    """The schema DDL TypeScript's golden test writes, written alike, for each engine."""
    span = range_index("Session", ["lastActivityAt", "sessionId"])
    unique = uniqueness_constraint("Person", "uuid")
    pair = uniqueness_constraint("Person", ["email", "tenant"])
    vector = vector_index(
        "Chunk",
        "embedding",
        1536,
        "COSINE",
        hnsw_m=16,
        hnsw_ef_construction=200,
        engine_options=[
            EngineVectorOptions("Neo4j", quantization_enabled=False),
            EngineVectorOptions("FalkorDB", ef_runtime=10, hnsw_m=32),
        ],
    )
    plain = vector_index("Chunk", "summary", 384, "EUCLIDEAN")
    text = full_text_index("Article", ["title", "body"])
    single = full_text_index("Article", "title")

    def shown(statements: list[Any]) -> str:
        return "\n".join(
            it
            if isinstance(it, str)
            else json.dumps(
                {"native": it.native, "label": it.label, "properties": list(it.properties)}, separators=(",", ":")
            )
            for it in statements
        )

    return {
        "schema.listRanges": grammar.list_ranges.statement,
        "schema.listUniques": grammar.list_uniques.statement,
        "schema.listVectors": grammar.list_vectors.statement,
        "schema.listFullTexts": grammar.list_full_texts.statement,
        "schema.createRange": shown(grammar.create_range(span)),
        "schema.dropRange": shown(grammar.drop_range(span)),
        "schema.createUnique": shown(grammar.create_unique(unique)),
        "schema.createUnique.pair": shown(grammar.create_unique(pair)),
        "schema.dropUnique": shown(grammar.drop_unique(pair)),
        "schema.createVector": shown(grammar.create_vector(vector)),
        "schema.createVector.plain": shown(grammar.create_vector(plain)),
        "schema.dropVector": shown(grammar.drop_vector(vector)),
        "schema.createFullText": shown(grammar.create_full_text(text)),
        "schema.createFullText.single": shown(grammar.create_full_text(single)),
        "schema.dropFullText": shown(grammar.drop_full_text(text, None)),
        "schema.dropFullText.named": shown(grammar.drop_full_text(text, "given_name")),
        "schema.conflicts": grammar.conflicts(pair, 10),
    }


@pytest.mark.parametrize(
    "engine, grammar, ddl",
    [
        ("NEO4J", NEO4J_GRAMMAR, NEO4J_SCHEMA),
        ("MEMGRAPH", MEMGRAPH_GRAMMAR, MEMGRAPH_SCHEMA),
        ("FALKORDB", FALKORDB_GRAMMAR, FALKORDB_SCHEMA),
    ],
)
def test_writes_what_typescript_writes(engine: str, grammar: CypherGrammar, ddl: SchemaGrammar):
    expect_golden(engine, {**statements(grammar), **schema(ddl)})


def test_writes_what_typescript_writes_for_oracle():
    expect_golden("ORACLE", {**oracle_statements(), **schema(schema_grammar_for(DatabaseType.ORACLE))})


def expect_golden(engine: str, statements: dict[str, str]) -> None:
    assert statements.keys() == GOLDEN[engine].keys()
    for name, text in statements.items():
        assert text == GOLDEN[engine][name], name
