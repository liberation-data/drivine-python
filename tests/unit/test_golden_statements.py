"""
The statements the backend writes for the test models, checked against those TypeScript's Drivine writes for the same
models: tests/golden/statements.json is written by its test/1.Unit/golden/GoldenStatements.spec.ts.
"""

import json
from pathlib import Path

import pytest

from drivine import CascadeType, ModelRegistry, RemovedTargets, SaveOptions
from drivine.backend.cypher_backend import CypherBackend
from drivine.backend.cypher_grammar import FALKORDB_GRAMMAR, MEMGRAPH_GRAMMAR, NEO4J_GRAMMAR, CypherGrammar
from drivine.backend.graph_statement import GraphStatement
from tests.models import (
    ActorStats,
    ActorWithLatestMovie,
    ActorWithMovies,
    Customer,
    Location,
    LocationAncestors,
    LocationHierarchy,
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
    return {name: it.text for name, it in cases.items()}


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
