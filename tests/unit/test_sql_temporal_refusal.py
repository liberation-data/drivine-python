"""
Postgres and Oracle refuse a query DSL comparison with a date, datetime or time, and an order by a field declared one,
by name: store layout 2 holds such a value as its tag, which their DSL would compare by its text, until the DSL takes
the shared module's fragments (drivine-cypher-spec#235, steps 3 and 4). SQLite's DSL is the module's fragments since
step 2, which compare a date by its tag, and renders them.
"""

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from typing import Annotated, Any

import pytest

from drivine import (
    DrivineError,
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
    equal_to,
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

REFUSING = {"Postgres": PostgresBackend(), "Oracle": OracleBackend(OracleGraph())}
SQLITE = SqliteBackend(SqliteGraph())


def labels(type_: type) -> list[str]:
    return registry.fragment(type_).labels


def refused(store: str) -> str:
    return (
        rf"^Temporal filters via the query DSL aren't supported on {store} yet \(.*\): use Cypher "
        r"\(drivine-cypher-spec#235\)\.$"
    )


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


@pytest.mark.parametrize("store", REFUSING)
@pytest.mark.parametrize("where", TEMPORAL.values(), ids=TEMPORAL.keys())
def test_a_comparison_with_a_temporal_value_is_refused_by_name(store: str, where: Callable[[], Any]):
    with pytest.raises(DrivineError, match=refused(store)):
        REFUSING[store].render_where(where(), model, labels)


@pytest.mark.parametrize("store", REFUSING)
@pytest.mark.parametrize("where", IN_A_VIEW.values(), ids=IN_A_VIEW.keys())
def test_a_comparison_with_a_temporal_value_in_a_view_is_refused_by_name(store: str, where: Callable[[], Any]):
    with pytest.raises(DrivineError, match=refused(store)):
        REFUSING[store].render_where(where(), view, labels)


@pytest.mark.parametrize("store", REFUSING)
def test_the_refusal_names_the_property_and_the_value(store: str):
    with pytest.raises(DrivineError, match=r"\(on is compared with datetime\.date\(2024, 1, 2\)\)"):
        REFUSING[store].render_where(q.on.eq(DAY), model, labels)


@pytest.mark.parametrize("store", REFUSING)
@pytest.mark.parametrize("where", PLAIN.values(), ids=PLAIN.keys())
def test_a_comparison_with_no_temporal_value_renders(store: str, where: Callable[[], Any]):
    written, _ = REFUSING[store].render_where(where(), model, labels)
    assert written


@pytest.mark.parametrize("store", REFUSING)
@pytest.mark.parametrize("field", ["on", "at", "starts"])
def test_an_order_by_a_field_declared_temporal_is_refused_by_name(store: str, field: str):
    with pytest.raises(DrivineError, match=refused(store)) as raised:
        REFUSING[store].render_query(Query(order_by=getattr(q, field).desc()), model, labels)
    assert f"an order is by {field}" in str(raised.value)
    # By its stored name, the field is the one that declares it.
    with pytest.raises(DrivineError, match=refused(store)):
        REFUSING[store].render_query(Query(order_by=q.property(field).asc()), model, labels)
    # By a view's root's field.
    with pytest.raises(DrivineError, match=refused(store)):
        REFUSING[store].render_query(Query(order_by=getattr(qv.dated, field).asc()), view, labels)


@pytest.mark.parametrize("store", REFUSING)
def test_an_order_expression_that_reads_a_field_declared_temporal_is_refused_by_name(store: str):
    for term in (case_of([above(q.on, q.at).then(0)], 1).asc(), coalesce(q.at, 0).desc()):
        with pytest.raises(DrivineError, match=refused(store)) as raised:
            REFUSING[store].render_query(Query(order_by=term), model, labels)
        assert "an order's expression reads" in str(raised.value)


@pytest.mark.parametrize("store", REFUSING)
def test_an_order_expression_that_tests_whether_a_field_declared_temporal_has_a_value_renders(store: str):
    # Whether it has a value compares nothing, as is_null doesn't, so it isn't refused.
    for term in (case_of([has_value(q.on).then(0)], 1).asc(), case_of([has_no_value(q.at).then(0)], 1).desc()):
        assert REFUSING[store].render_query(Query(order_by=term), model, labels).order_by


@pytest.mark.parametrize("store", REFUSING)
def test_a_seek_or_an_order_expression_with_a_temporal_value_is_refused_by_name(store: str):
    # An order by a property no field declares, so only the cursor's value is temporal.
    seek = Query(order_by=q.property("other").asc(), seek=q.property("other").after(DAY))
    with pytest.raises(DrivineError, match=refused(store)):
        REFUSING[store].render_query(seek, model, labels)
    ranked = Query(order_by=case_of([equal_to(q.name, DAY).then(0)], 1).asc())
    with pytest.raises(DrivineError, match=refused(store)):
        REFUSING[store].render_query(ranked, model, labels)


@pytest.mark.parametrize("store", REFUSING)
def test_an_order_by_a_jsonpacked_or_plain_field_renders(store: str):
    for term in (q.packed.asc(), q.name.asc(), q.property("other").asc()):
        assert REFUSING[store].render_query(Query(order_by=term), model, labels).order_by


@pytest.mark.parametrize("where", TEMPORAL.values(), ids=TEMPORAL.keys())
def test_sqlite_renders_a_comparison_with_a_temporal_value(where: Callable[[], Any]):
    written, _ = SQLITE.render_where(where(), model, labels)
    assert written


def test_sqlite_renders_an_order_by_and_a_seek_after_a_temporal_value():
    for field in ("on", "at", "starts"):
        assert SQLITE.render_query(Query(order_by=getattr(q, field).desc()), model, labels).order_by
    seek = Query(order_by=q.on.asc(), seek=q.on.after(DAY))
    assert SQLITE.render_query(seek, model, labels).where


def test_a_graph_engine_renders_a_comparison_with_a_temporal_value_and_an_order_by_one():
    # The refusal is the SQL stores' own: the graph engines compare and order a temporal value as it is.
    neo4j = CypherBackend(NEO4J_GRAMMAR)
    for where in TEMPORAL.values():
        written, _ = neo4j.render_where(where(), model, labels)
        assert written
    assert neo4j.render_query(Query(order_by=q.on.desc()), model, labels).order_by


def test_sqlite_renders_an_order_expression_that_reads_a_field_declared_temporal():
    assert SQLITE.render_query(Query(order_by=coalesce(q.at, 0).desc()), model, labels).order_by


GROUPS: dict[str, Callable[[], GroupQuery]] = {
    "a grouped read's where": lambda: GroupQuery({"name": key("name")}, {"all": count()}, where=q.on.gt(DAY)),
    "a filtered count": lambda: GroupQuery({"name": key("name")}, {"late": count(q.at.gt(AT))}),
    "a filtered sum": lambda: GroupQuery({"name": key("name")}, {"days": sum_("id", q.on.lte(DAY))}),
}


@pytest.mark.parametrize("store", REFUSING)
@pytest.mark.parametrize("grouped", GROUPS.values(), ids=GROUPS.keys())
def test_a_grouped_read_that_compares_with_a_temporal_value_is_refused_by_name(
    store: str, grouped: Callable[[], GroupQuery]
):
    with pytest.raises(DrivineError, match=refused(store)):
        REFUSING[store].groups(model, grouped(), {}, labels)


@pytest.mark.parametrize("grouped", GROUPS.values(), ids=GROUPS.keys())
def test_sqlite_renders_a_grouped_read_that_compares_with_a_temporal_value(grouped: Callable[[], GroupQuery]):
    assert SQLITE.groups(model, grouped(), {}, labels).text


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
def test_sqlite_compares_a_moment_by_its_tag_bound_by_the_renderers_names(where: Callable[[], Any]):
    written, bindings = SQLITE.render_where(where(), model, labels)
    assert "n_.props" in written
    assert "$_cy__q0" in written
    assert list(bindings) == ["_cy__q0"]
    assert TAGGED in bindings["_cy__q0"]


def test_sqlite_binds_a_list_of_moments_as_the_list_of_their_tags():
    assert SQLITE.render_where(q.at.in_([FIRST]), model, labels)[1] == {"_cy__q0": f"[{TAGGED}]"}


def test_sqlite_compares_a_moment_by_its_tag_in_a_seek_and_in_an_order_expression_named_apart_there():
    seek = SQLITE.render_query(Query(order_by=q.at.asc(), seek=q.at.after(FIRST)), model, labels)
    assert seek.bindings == {"_cy__q0": TAGGED}
    order = SQLITE.render_query(Query(order_by=case_of([above(q.at, FIRST).then(1)], 2).asc()), model, labels)
    assert order.where is None
    assert order.order_by is not None and "$_cy_f1__q0" in order.order_by
    assert order.bindings == {"_cy_f1__q0": TAGGED, "_cy_f1__q1": "1", "_cy_f1__q2": "2"}


def test_sqlite_compares_a_moment_by_its_tag_in_a_grouped_reads_where_and_in_a_filtered_aggregate():
    grouped = GroupQuery({"name": key(q.name)}, {"since": count(q.at.gt(FIRST))}, where=q.at.gte(FIRST))
    statement = SQLITE.groups(model, grouped, {}, labels)
    # The aggregates' filters are written before the where, each numbered on from the one before.
    assert statement.bindings == {"_cy__q0": TAGGED, "_cy__q1": TAGGED}
    assert "$_cy__q0" in statement.text and "$_cy__q1" in statement.text


def test_sqlite_binds_a_jsonpacked_propertys_value_date_or_not_as_the_text_it_is_packed_as():
    def bound(where: Any) -> list[Any]:
        return list(SQLITE.render_where(where, model, labels)[1].values())

    # As a save packs it (see save_statement_builder.packed), then as the JSON text a fragment binds.
    assert bound(q.packed.eq(DAY)) == [json.dumps(packed(DAY))]
    assert bound(q.packed.eq({"n": 1})) == [json.dumps(packed({"n": 1}))]
    assert bound(q.name.eq("two")) == ['"two"']
    assert bound(q.at.is_not_null()) == []
