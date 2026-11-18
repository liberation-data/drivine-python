"""
A query DSL comparison with a date, datetime or time, and an order by a field declared one, on each SQL store: its DSL
is the shared module's fragments (drivine-cypher-spec#235), which compare a date by its tag, as store layout 2 holds it,
so no SQL store refuses one (drivine#771). Drivine for TypeScript's SqlTemporalFilters.spec.
"""

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from typing import Annotated, Any

import pytest

from drivine import (
    GraphView,
    JsonPacked,
    ModelRegistry,
    NodeFragment,
    NodeId,
    Relationship,
    RelationshipFragment,
    Root,
    TargetNode,
    above,
    case_of,
    coalesce,
    count,
    has_no_value,
    has_value,
    key,
    refs,
    sum_,
)
from drivine.backend.cypher_backend import CypherBackend
from drivine.backend.cypher_grammar import NEO4J_GRAMMAR
from drivine.backend.oracle.oracle_backend import OracleBackend
from drivine.backend.oracle.oracle_graph import OracleGraph
from drivine.backend.postgres.postgres_backend import PostgresBackend
from drivine.backend.save_statement_builder import packed
from drivine.backend.sqlite.sqlite_backend import SqliteBackend
from drivine.backend.sqlite.sqlite_graph import SqliteGraph
from drivine.dsl.query import Query
from drivine.manager.group_query import GroupQuery


class Dated(NodeFragment, labels=["Dated"]):
    id: Annotated[str, NodeId()]
    on: date | None = None
    at: datetime | None = None
    starts: time | None = None
    days: list[date] | None = None
    packed: Annotated[date | None, JsonPacked()] = None
    name: str | None = None


class Met(RelationshipFragment):
    since: date | None = None
    to: Annotated[Dated, TargetNode()]


class Meetings(GraphView):
    dated: Annotated[Dated, Root()]
    met: Annotated[list[Met], Relationship("MET")] = []
    knows: Annotated[list[Dated], Relationship("KNOWS")] = []


registry = ModelRegistry()
model = registry.fragment(Dated)
view = registry.view(Meetings)
q = refs(Dated)
qv = refs(Meetings)
DAY = date(2024, 1, 2)
AT = datetime(2024, 5, 6, 7, 8, 9, tzinfo=UTC)
NINE = time(9)

FRAGMENTED = {
    "SQLite": SqliteBackend(SqliteGraph()),
    "Postgres": PostgresBackend(),
    "Oracle": OracleBackend(OracleGraph()),
}
"""The stores whose query DSL is the shared module's fragments, which compare a date by its tag."""


def labels(type_: type) -> list[str]:
    return registry.fragment(type_).labels


TEMPORAL: dict[str, Callable[[], Any]] = {
    "eq": lambda: q.on.eq(DAY),
    "neq": lambda: q.on.neq(DAY),
    "gt": lambda: q.at.gt(AT),
    "gte": lambda: q.at.gte(AT),
    "lt": lambda: q.starts.lt(NINE),
    "lte": lambda: q.on.lte(DAY),
    "in": lambda: q.on.in_([DAY, date(2024, 1, 3)]),
    "in a temporal among others": lambda: q.on.in_(["2024-01-02", DAY]),
    "in a temporal beside None": lambda: q.on.in_([None, DAY]),
    "not in": lambda: q.at.not_in([AT]),
    "has_item": lambda: q.days.has_item(DAY),
    "a temporal list": lambda: q.days.eq([DAY]),
    "a map that holds one": lambda: q.name.eq({"from": DAY}),
    "a list that holds one deeper": lambda: q.name.eq([[DAY]]),
    "by stored name": lambda: q.property("on").eq(DAY),
    "a predicate": lambda: q.predicate("on", "GT", DAY),
}

IN_A_VIEW: dict[str, Callable[[], Any]] = {
    "the root's": lambda: qv.dated.on.gt(DAY),
    "a relationship's property": lambda: qv.met.since.gt(DAY),
    "a relationship's property in any": lambda: qv.met.any(lambda it: it.since.gt(DAY)),
    "a related node's": lambda: qv.knows.on.eq(DAY),
}

PLAIN: dict[str, Callable[[], Any]] = {
    "text": lambda: q.name.eq("Ada"),
    "the ISO text of a date": lambda: q.on.eq("2024-01-02"),
    "in": lambda: q.name.in_(["a", "b"]),
    "in of None alone": lambda: q.on.in_([None]),
    "is_null": lambda: q.on.is_null(),
    "a map of text": lambda: q.name.eq({"from": "2024-01-02"}),
    "a JsonPacked date": lambda: q.packed.eq(DAY),
}


@pytest.mark.parametrize("store", FRAGMENTED)
@pytest.mark.parametrize("where", PLAIN.values(), ids=PLAIN.keys())
def test_a_comparison_with_no_temporal_value_renders(store: str, where: Callable[[], Any]):
    written, _ = FRAGMENTED[store].render_where(where(), model, labels)
    assert written


@pytest.mark.parametrize("store", FRAGMENTED)
def test_an_order_expression_that_tests_whether_a_field_declared_temporal_has_a_value_renders(store: str):
    for term in (case_of([has_value(q.on).then(0)], 1).asc(), case_of([has_no_value(q.at).then(0)], 1).desc()):
        assert FRAGMENTED[store].render_query(Query(order_by=term), model, labels).order_by


@pytest.mark.parametrize("store", FRAGMENTED)
def test_an_order_by_a_jsonpacked_or_plain_field_renders(store: str):
    for term in (q.packed.asc(), q.name.asc(), q.property("other").asc()):
        assert FRAGMENTED[store].render_query(Query(order_by=term), model, labels).order_by


@pytest.mark.parametrize("store", FRAGMENTED)
@pytest.mark.parametrize("where", TEMPORAL.values(), ids=TEMPORAL.keys())
def test_the_fragments_render_a_comparison_with_a_temporal_value(store: str, where: Callable[[], Any]):
    written, _ = FRAGMENTED[store].render_where(where(), model, labels)
    assert written


@pytest.mark.parametrize("store", FRAGMENTED)
def test_the_fragments_render_an_order_by_and_a_seek_after_a_temporal_value(store: str):
    for field in ("on", "at", "starts"):
        assert FRAGMENTED[store].render_query(Query(order_by=getattr(q, field).desc()), model, labels).order_by
    seek = Query(order_by=q.on.asc(), seek=q.on.after(DAY))
    assert FRAGMENTED[store].render_query(seek, model, labels).where


def test_a_graph_engine_renders_a_comparison_with_a_temporal_value_and_an_order_by_one():
    # As the SQL stores do, the graph engines compare and order a temporal value as it is.
    neo4j = CypherBackend(NEO4J_GRAMMAR)
    for where in TEMPORAL.values():
        written, _ = neo4j.render_where(where(), model, labels)
        assert written
    assert neo4j.render_query(Query(order_by=q.on.desc()), model, labels).order_by


@pytest.mark.parametrize("store", FRAGMENTED)
def test_the_fragments_render_an_order_expression_that_reads_a_field_declared_temporal(store: str):
    assert FRAGMENTED[store].render_query(Query(order_by=coalesce(q.at, 0).desc()), model, labels).order_by


GROUPS: dict[str, Callable[[], GroupQuery]] = {
    "a grouped read's where": lambda: GroupQuery({"name": key("name")}, {"all": count()}, where=q.on.gt(DAY)),
    "a filtered count": lambda: GroupQuery({"name": key("name")}, {"late": count(q.at.gt(AT))}),
    "a filtered sum": lambda: GroupQuery({"name": key("name")}, {"days": sum_("id", q.on.lte(DAY))}),
}


@pytest.mark.parametrize("store", FRAGMENTED)
@pytest.mark.parametrize("grouped", GROUPS.values(), ids=GROUPS.keys())
def test_the_fragments_render_a_grouped_read_that_compares_with_a_temporal_value(
    store: str, grouped: Callable[[], GroupQuery]
):
    assert FRAGMENTED[store].groups(model, grouped(), {}, labels).text


TAGGED = json.dumps(
    {"$t": "datetime", "v": "2026-01-01T00:00:00Z", "s": "10000020260101000000000000000064800"}, separators=(",", ":")
)
"""The JSON a fragment binds a moment as: the tag store layout 2 holds it as (drivine-cypher-spec#161)."""

FIRST = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "where",
    [
        lambda: q.at.eq(FIRST),
        lambda: q.at.gt(FIRST),
        lambda: q.at.not_in([FIRST]),
        lambda: q.days.has_item(FIRST),
        lambda: q.property("at").eq(FIRST),
    ],
    ids=["eq", "gt", "not_in", "has_item", "by stored name"],
)
@pytest.mark.parametrize("store", FRAGMENTED)
def test_the_fragments_compare_a_moment_by_its_tag_bound_by_the_renderers_names(store: str, where: Callable[[], Any]):
    written, bindings = FRAGMENTED[store].render_where(where(), model, labels)
    assert "n_.props" in written
    assert "$_cy__q0" in written
    assert list(bindings) == ["_cy__q0"]
    assert TAGGED in bindings["_cy__q0"]


@pytest.mark.parametrize("store", FRAGMENTED)
def test_the_fragments_bind_a_list_of_moments_as_the_list_of_their_tags(store: str):
    assert FRAGMENTED[store].render_where(q.at.in_([FIRST]), model, labels)[1] == {"_cy__q0": f"[{TAGGED}]"}


@pytest.mark.parametrize("store", FRAGMENTED)
def test_the_fragments_compare_a_moment_by_its_tag_in_a_seek_and_in_an_order_expression_named_apart_there(store: str):
    seek = FRAGMENTED[store].render_query(Query(order_by=q.at.asc(), seek=q.at.after(FIRST)), model, labels)
    assert seek.bindings == {"_cy__q0": TAGGED}
    order = FRAGMENTED[store].render_query(
        Query(order_by=case_of([above(q.at, FIRST).then(1)], 2).asc()), model, labels
    )
    assert order.where is None
    assert order.order_by is not None and "$_cy_f1__q0" in order.order_by
    assert order.bindings == {"_cy_f1__q0": TAGGED, "_cy_f1__q1": "1", "_cy_f1__q2": "2"}


@pytest.mark.parametrize("store", FRAGMENTED)
def test_the_fragments_compare_a_moment_by_its_tag_in_a_grouped_reads_where_and_in_a_filtered_aggregate(store: str):
    grouped = GroupQuery({"name": key(q.name)}, {"since": count(q.at.gt(FIRST))}, where=q.at.gte(FIRST))
    statement = FRAGMENTED[store].groups(model, grouped, {}, labels)
    # The aggregates' filters are written before the where, each numbered on from the one before.
    assert statement.bindings == {"_cy__q0": TAGGED, "_cy__q1": TAGGED}
    assert "$_cy__q0" in statement.text and "$_cy__q1" in statement.text


@pytest.mark.parametrize("store", FRAGMENTED)
def test_the_fragments_bind_a_jsonpacked_propertys_value_date_or_not_as_the_text_it_is_packed_as(store: str):
    def bound(where: Any) -> dict[str, Any]:
        return FRAGMENTED[store].render_where(where, model, labels)[1]

    def text(it: str) -> dict[str, Any]:
        """On Oracle an equality with a text binds its key alone, as plain text (drivine#761): its JSON elsewhere."""
        return {"_cyk__q0": it} if store == "Oracle" else {"_cy__q0": json.dumps(it)}

    # As a save packs it (see save_statement_builder.packed), then as the text a fragment binds.
    assert bound(q.packed.eq(DAY)) == text(packed(DAY))
    assert bound(q.packed.eq({"n": 1})) == text(packed({"n": 1}))
    assert bound(q.name.eq("two")) == text("two")
    assert bound(q.at.is_not_null()) == {}
