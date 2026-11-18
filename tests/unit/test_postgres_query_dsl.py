"""
The query DSL on Postgres as the shared module's fragments (drivine-cypher-spec#235, step 3), as on SQLite: the Cypher
the DSL writes for Neo4j, its filter and its seek each compiled whole over the row `n_`, and each order, group key and
aggregate the library writes a fragment named apart. A grouped read's least or greatest value is the group's column, as
drivine4j's PostgresSelect gives it. Pinned here as SQL, which needs no database; the Postgres integration tests run
them. A port of Drivine for TypeScript's PostgresQueryDsl.spec.
"""

import re
import uuid
from typing import Annotated, Any

from pydantic import BaseModel

from drivine import (
    GraphProperty,
    Hop,
    ModelRegistry,
    NodeFragment,
    NodeId,
    NodeStamp,
    above,
    case_of,
    count,
    key,
    max_,
    min_,
    path_key,
    refs,
    sum_,
)
from drivine.backend.graph_statement import GraphStatement
from drivine.backend.page import Page
from drivine.backend.postgres.postgres_backend import PostgresBackend
from drivine.backend.postgres.postgres_graph import PostgresGraph
from drivine.dsl.query import Query
from drivine.manager.group_operations import GroupOperations


class Scored(NodeFragment, labels=["Scored"]):
    id: Annotated[str, NodeId()]
    name: str | None = None
    score: Annotated[float | None, GraphProperty("$score")] = None


class Stamped(NodeFragment, labels=["Stamped"]):
    id: Annotated[str, NodeId()]
    stamp: Annotated[str | None, NodeStamp()] = None


class Judge(NodeFragment, labels=["Judge"]):
    code: Annotated[str, NodeId()]


registry = ModelRegistry()
scored = registry.fragment(Scored)
backend = PostgresBackend(PostgresGraph())
q = refs(Scored)


def labels(type_: type) -> list[str]:
    return registry.fragment(type_).labels


async def grouped(**spec: Any) -> GraphStatement:
    """The grouped read `spec` asks of Scored on Postgres, as the statement it is written as."""
    statements: list[GraphStatement] = []

    async def query(statement: GraphStatement) -> list[Any]:
        statements.append(statement)
        return []

    await GroupOperations(registry, query, None, backend).load(Scored, **spec)  # type: ignore[arg-type]
    return statements[0]


def test_compiles_the_filter_and_the_seek_whole_over_n_and_reads_a_property_whose_name_begins_with_dollar_as_held():
    query = backend.render_query(
        Query(where=q.score.gt(1), order_by=[q.score.desc(), q.id.asc()], seek=[q.score.after(5), q.id.after("s1")]),
        scored,
        labels,
    )
    assert query.where is not None and query.order_by is not None
    assert "(n_.props -> '$$score')" in query.where
    assert "'$score'" not in query.where
    assert "(n_.props -> '$$score')" in query.order_by
    assert list(query.bindings) == ["_cy__q0", "_cy__q1", "_cy__q2"]
    assert query.labels_tested == ()
    statement = backend.load_all_fragments(scored, query.where, Page(order_by=query.order_by))
    assert "FROM drivine_node n_\nWHERE " in statement.text


def test_compares_a_propertys_equality_over_its_json_the_expression_a_range_index_is_made_over():
    query = backend.render_query(Query(where=q.name.eq("Ada")), scored, labels)
    assert query.where == "(n_.props -> 'name') = CAST($_cy__q0 AS jsonb)"
    assert query.bindings == {"_cy__q0": '"Ada"'}


def test_compares_and_orders_text_by_its_code_points_in_the_c_collation_not_the_database_collation():
    query = backend.render_query(
        Query(where=q.name.gt("B"), order_by=[q.name.asc()], seek=[q.name.after("B")]), scored, labels
    )
    assert query.where is not None and query.order_by is not None
    assert """(n_.props ->> 'name') COLLATE "C" > (CAST($_cy__q0 AS jsonb) #>> '{}')""" in query.where
    assert """(n_.props ->> 'name') COLLATE "C" > (CAST($_cy__q1 AS jsonb) #>> '{}')""" in query.where
    assert """(n_.props ->> 'name') END) COLLATE "C\"""" in query.order_by


def test_filters_and_orders_by_a_nodes_stamp_as_its_rows_stamp_column_not_a_property_of_it():
    stamped = registry.fragment(Stamped)
    s = refs(Stamped)
    query = backend.render_query(Query(where=s.stamp.eq("a:b"), order_by=[s.stamp.asc()]), stamped, labels)
    assert query.where == "to_jsonb(n_.stamp) = CAST($_cy__q0 AS jsonb)"
    assert query.order_by is not None and "to_jsonb(n_.stamp)" in query.order_by
    assert "__drivine.stamp" not in f"{query.where} {query.order_by}"


def test_places_an_orders_nulls_by_a_term_of_whether_it_is_null_before_it_its_parameters_named_apart_in_each():
    query = backend.render_query(Query(order_by=[q.name.asc().nulls_first(), q.id.desc()]), scored, labels)
    assert query.order_by is not None
    # The null term first, descending for nulls first, then the property's terms.
    assert query.order_by.startswith("(COALESCE(jsonb_typeof((n_.props -> 'name')), 'null') = 'null') DESC")
    assert query.bindings == {}
    # An order that binds values binds them in its null term (f1) and in its own terms (f2), each named apart.
    bound = backend.render_query(
        Query(order_by=case_of([above(q.score, 3).then(1)], 2).asc().nulls_first()), scored, labels
    )
    assert bound.order_by is not None
    assert bound.order_by.startswith("(COALESCE(jsonb_typeof(CASE WHEN")
    assert "$_cy_f1__q0" in bound.order_by
    assert "$_cy_f2__q0" in bound.order_by
    assert bound.bindings == {
        "_cy_f1__q0": "3",
        "_cy_f1__q1": "1",
        "_cy_f1__q2": "2",
        "_cy_f2__q0": "3",
        "_cy_f2__q1": "1",
        "_cy_f2__q2": "2",
    }


def test_counts_and_deletes_over_n_and_reads_a_raw_where_over_n_as_n_():
    where = backend.written("n.name = 'Ada'")
    assert where == "(n_.props ->> 'name') = 'Ada'"
    assert "FROM drivine_node n_\nWHERE " in backend.count_fragments(scored, where).text
    assert "DELETE FROM drivine_node n_\nWHERE " in backend.delete_all(scored, where).text
    # A write's own reads keep the row `n`.
    assert "FROM drivine_node n\nWHERE " in backend.held_properties(scored).text
    assert "FROM drivine_node n\nWHERE " in backend.found_stamp(scored).text


def test_reads_a_raw_wheres_property_whose_name_begins_with_dollar_by_the_name_the_layout_holds_it_by():
    assert backend.written("n.`$score` > 1") == "(n_.props ->> '$$score') > 1"


def test_writes_a_patchs_condition_over_n_a_value_in_the_text_of_a_quoted_name_left_as_it_is():
    where, bindings = backend.render_where(q.score.eq(2), scored, labels)
    patch = backend.patch_one(scored, where)
    parameters: Any = patch.spec({"id": "s1", "set": {"name": "x"}, "vectors": {}, **bindings, "score": 9}).parameters
    patched_if: str = parameters["_patchedIf"]
    assert "FROM drivine_node n_ WHERE n_.nid = $1 AND (" in patched_if
    assert "'$$score'" in patched_if
    assert "$_cy__q0" not in patched_if


async def test_keys_and_adds_a_property_whose_name_begins_with_dollar_as_the_layout_holds_it():
    statement = await grouped(keys={"score": key("$score")}, aggregates={"total": sum_("$score"), "all": count()})
    assert "(n_.props -> '$$score') AS k0" in statement.text
    assert (
        "SUM(CASE WHEN jsonb_typeof((n_.props -> '$$score')) = 'number' THEN (n_.props ->> '$$score')::numeric END)"
    ) in statement.text
    assert "'$score'" not in statement.text
    # A property key is ordered through the group's node of least nid, joined back.
    assert "MIN(n_.nid) AS k0r" in statement.text
    assert "LEFT JOIN drivine_node k0_ ON k0_.nid = g.k0r" in statement.text


async def test_gives_a_least_or_greatest_value_as_the_groups_column_its_node_joined_back_only_to_compare_or_order_it():
    statement = await grouped(
        where=q.name.eq("Ada"),
        keys={"name": key(q.name)},
        aggregates={"low": min_(q.score), "high": max_(q.score, q.score.lt(100)), "all": count()},
        having=[("high", "gte", 3), ("all", "gte", 1)],
        order_by=[("high", "desc")],
    )
    text = statement.text
    # The first of the group's values in Cypher's order, of those that have one and meet the filter.
    assert re.search(
        r"\(array_agg\(\(n_\.props -> '\$\$score'\) ORDER BY .*\) FILTER \(WHERE .*\)\)\[1\] AS a0, ", text
    )
    assert re.search(
        r"\(array_agg\(\(n_\.props -> '\$\$score'\) ORDER BY .* DESC\) FILTER \(WHERE .*\)\)\[1\] AS a1", text
    )
    # The node that has the greatest, which the groups are filtered and ordered by, beside it; none for the least.
    assert re.search(r"\(array_agg\(n_\.nid ORDER BY .* DESC\) FILTER \(WHERE .*\)\)\[1\] AS a1r", text)
    assert "AS a0r" not in text
    assert "a0_" not in text
    assert "LEFT JOIN drivine_node a1_ ON a1_.nid = g.a1r" in text
    assert "'a0', g.a0, 'a1', g.a1" in text
    # The having of a held value is the module's condition over its node; a count's stays the group's.
    assert re.search(r"\nWHERE .*a1_\.props.*\$_cy_f\d+_value", text)
    assert "HAVING COUNT(*) >= CAST($_having1 AS numeric)" in text
    assert re.search(r"\nORDER BY .*a1_\.props", text)
    # The where and the aggregate's filter keep the renderer's names; the library's own fragments are named apart, in
    # the order they are written: the key (f1), each held value's presence, order and value (f2 to f7), the having (f8).
    assert sorted(statement.bindings) == ["_cy__q0", "_cy__q1", "_cy_f8_value", "_having1"]
    assert statement.bindings["_cy_f8_value"] == "3"


async def test_orders_a_path_key_through_its_node_joined_back_by_the_nodes_id_as_cypher_orders_it():
    statement = await grouped(keys={"judge": path_key(Judge, [Hop("JUDGED_BY")])}, aggregates={"all": count()})
    assert "LEFT JOIN drivine_node k0_ ON k0_.nid = g.k0" in statement.text
    assert re.search(r"\nORDER BY .*\(k0_\.props -> 'code'\)", statement.text)


async def test_reads_a_raw_where_over_n_as_the_row_n_a_read_of_the_dsl_calls_its_root_in_a_grouped_reads_too():
    statement = await grouped(
        where="n.name = 'Ada'", keys={"name": key("name")}, aggregates={"named": count("n.props IS NOT NULL")}
    )
    assert "AND ((n_.props ->> 'name') = 'Ada'))" in statement.text
    assert "SUM(CASE WHEN n_.props IS NOT NULL THEN 1 ELSE 0 END)" in statement.text


def test_binds_a_callers_name_beside_the_dsls_own_which_the_module_names_apart():
    # The DSL's `$_q0` is bound as `_cy__q0`, so a raw `$_q0` of the caller's is a name of its own.
    query = backend.render_query(Query(where=q.name.eq("Ada")), scored, labels)
    assert "_q0" not in query.bindings
    assert "$_q0" not in (query.where or "").replace("$_cy__q0", "")


class Place(BaseModel):
    city: str


class Keyed(NodeFragment, labels=["Keyed"]):
    id: Annotated[uuid.UUID, NodeId()]
    place: Place | None = None


def test_binds_a_uuid_and_a_model_as_the_json_a_save_holds_them_as_in_a_condition_and_a_seek():
    # A save holds a UUID as its text and a model as the JSON pydantic writes it (value_layout.write): the old renderer
    # bound them so, which the module's parameters can't take as they are.
    keyed = registry.fragment(Keyed)
    k = refs(Keyed)
    one = uuid.UUID(int=1)
    query = backend.render_query(
        Query(where=[k.id.in_([one]), k.place.eq(Place(city="Oslo"))], order_by=[k.id.asc()], seek=[k.id.after(one)]),
        keyed,
        labels,
    )
    assert query.bindings == {
        "_cy__q0": f'["{one}"]',
        "_cy__q1": '{"city":"Oslo"}',
        "_cy__q2": f'"{one}"',
    }
