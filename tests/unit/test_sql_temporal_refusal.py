"""
Postgres and Oracle refuse a query DSL comparison with a date, datetime or time, and an order by a field declared one,
by name: store layout 2 holds such a value as its tag, which their DSL would compare by its text, until the DSL takes
the shared module's fragments (drivine-cypher-spec#235). SQLite's DSL reads the tag, and renders them.
"""

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
    key,
    refs,
    sum_,
)
from drivine.backend.cypher_backend import CypherBackend
from drivine.backend.cypher_grammar import NEO4J_GRAMMAR
from drivine.backend.oracle.oracle_backend import OracleBackend
from drivine.backend.oracle.oracle_graph import OracleGraph
from drivine.backend.postgres.postgres_backend import PostgresBackend
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
