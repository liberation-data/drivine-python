"""The parentheses of a query's WHERE, as TypeScript's Drivine's QueryRenderer.spec.ts holds them."""

from collections.abc import Callable
from typing import Any

from drivine import ModelRegistry, all_of, any_of, not_, plus, refs
from drivine.backend.cypher_grammar import FALKORDB_GRAMMAR, MEMGRAPH_GRAMMAR, NEO4J_GRAMMAR
from drivine.dsl.condition import all_of_one
from drivine.dsl.model_scope import scope_of
from drivine.dsl.query import Query
from drivine.dsl.query_renderer import QueryRenderer, list_of, root_property
from drivine.dsl.root_key import RootKey
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


def test_gives_a_cursors_keyset_and_an_order_terms_cypher_with_the_values_each_binds():
    # A SQL store asks the shared module for each as a fragment (drivine.backend.sql.fragments), as Neo4j's writes it.
    m = refs(Movie)
    query = Query(
        where=m.title.eq("a"),
        order_by=[m.released.desc(), m.title.asc()],
        seek=[m.released.after(1999), m.title.after("m")],
    )
    scope = scope_of(movie)
    renderer = QueryRenderer(NEO4J_GRAMMAR, labels)
    renderer.where(all_of_one(query.where), scope, "n")
    keys = [RootKey(it, root_property(it.path, scope, "An order")) for it in list_of(query.order_by)]
    keyset, bound = renderer.keyset(keys, list_of(query.seek), query, "n")
    assert keyset == (
        "n.released IS NOT NULL AND n.title IS NOT NULL AND n.released <= $_q1 AND "
        "(n.released < $_q1 OR (n.released = $_q1 AND n.title > $_q2))"
    )
    assert bound == {"_q1": 1999, "_q2": "m"}
    assert QueryRenderer.render(query, movie, NEO4J_GRAMMAR, "n", labels).where == f"n.title = $_q0 AND {keyset}"
    assert renderer.order_expression(keys[0], scope, "n") == ("n.released", {})
    expression = RootKey(plus(m.released, 1).asc())
    assert renderer.order_expression(expression, scope, "n") == ("(n.released + $_q3)", {"_q3": 1})
