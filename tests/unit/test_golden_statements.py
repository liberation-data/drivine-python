"""
The statements the backend writes for the test models, checked against those TypeScript's Drivine writes for the same
models: tests/golden/statements.json is written by its test/1.Unit/golden/GoldenStatements.spec.ts.
"""

import json
from pathlib import Path

import pytest

from drivine import ModelRegistry
from drivine.backend.cypher_backend import CypherBackend
from drivine.backend.cypher_grammar import FALKORDB_GRAMMAR, MEMGRAPH_GRAMMAR, NEO4J_GRAMMAR, CypherGrammar
from tests.models import (
    ActorStats,
    ActorWithLatestMovie,
    ActorWithMovies,
    LocationAncestors,
    LocationHierarchy,
    Person,
    PersonCareer,
    Puppy,
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
    return {name: it.text for name, it in cases.items()}


@pytest.mark.parametrize(
    "engine, grammar", [("NEO4J", NEO4J_GRAMMAR), ("MEMGRAPH", MEMGRAPH_GRAMMAR), ("FALKORDB", FALKORDB_GRAMMAR)]
)
def test_writes_what_typescript_writes(engine: str, grammar: CypherGrammar):
    written = statements(grammar)
    assert written.keys() == GOLDEN[engine].keys()
    for name, text in written.items():
        assert text == GOLDEN[engine][name], name
