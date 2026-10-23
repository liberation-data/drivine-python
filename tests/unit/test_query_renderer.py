"""The parentheses of a query's WHERE, as TypeScript's Drivine's QueryRenderer.spec.ts holds them."""

from collections.abc import Callable
from typing import Any

from drivine import ModelRegistry, all_of, any_of, not_, refs
from drivine.backend.cypher_grammar import FALKORDB_GRAMMAR, MEMGRAPH_GRAMMAR, NEO4J_GRAMMAR
from drivine.dsl.query import Query
from drivine.dsl.query_renderer import QueryRenderer
from tests.models import Movie

registry = ModelRegistry()
movie = registry.fragment(Movie)


def labels(type_: type) -> list[str]:
    return registry.fragment(type_).labels


def where(fn: Callable[[Any], Any]) -> list[str | None]:
    """The WHERE each engine writes for the condition."""
    return [
        QueryRenderer.render(Query(where=fn(refs(Movie))), movie, grammar, "n", labels).where
        for grammar in (NEO4J_GRAMMAR, MEMGRAPH_GRAMMAR, FALKORDB_GRAMMAR)
    ]


def on_every(it: str) -> list[str]:
    return [it, it, it]


def test_writes_the_outermost_and_of_a_where_without_parentheses_and_keeps_those_precedence_needs():
    # FalkorDB 6 scans the label for a WHERE in parentheses it does not need (FalkorDB/FalkorDB#3331). The
    # parentheses go on every engine, where they change nothing.
    assert where(lambda m: m.title.eq("a")) == on_every("n.title = $_q0")
    assert where(lambda m: [any_of(m.title.eq("a"), m.title.eq("b")), m.released.eq(1999)]) == on_every(
        "(n.title = $_q0 OR n.title = $_q1) AND n.released = $_q2"
    )
    # An any_of at the top keeps its parentheses: a statement may join more to the WHERE by AND.
    assert where(lambda m: any_of(m.title.eq("a"), all_of(m.title.eq("b"), m.released.eq(1999)))) == on_every(
        "(n.title = $_q0 OR (n.title = $_q1 AND n.released = $_q2))"
    )
    assert where(lambda m: [not_(m.title.eq("a"), m.released.eq(1999)), m.title.eq("b")]) == on_every(
        "NOT (n.title = $_q0 AND n.released = $_q1) AND n.title = $_q2"
    )
    assert where(lambda m: not_(m.title.eq("a"))) == on_every("NOT (n.title = $_q0)")
    # An all_of in the outermost AND is a part of it, and needs no parentheses of its own.
    assert where(lambda m: [all_of(m.title.eq("a"), m.released.eq(1999)), m.title.eq("b")]) == on_every(
        "n.title = $_q0 AND n.released = $_q1 AND n.title = $_q2"
    )
    assert where(lambda m: all_of()) == on_every("true")


def test_joins_a_cursor_to_the_outermost_and_as_it_stands():
    m = refs(Movie)
    query = Query(
        where=[any_of(m.title.eq("a"), m.title.eq("b")), m.released.eq(1999)],
        order_by=m.title.asc(),
        seek=m.title.after("m"),
    )
    assert (
        QueryRenderer.render(query, movie, FALKORDB_GRAMMAR, "n", labels).where
        == "(n.title = $_q0 OR n.title = $_q1) AND n.released = $_q2 AND n.title > $_q3"
    )
