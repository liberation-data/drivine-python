"""
The query DSL on SQLite as the shared module's fragments (drivine-cypher-spec#235, step 2): the Cypher the DSL writes
for Neo4j, its filter and its seek each compiled whole over the row `n_`, and each order, group key and aggregate the
library writes a fragment named apart. A port of Drivine for TypeScript's SqliteQueryDsl.spec.
"""

import re
from typing import Annotated, Any

import pytest

from drivine import (
    DrivineError,
    GraphProperty,
    Hop,
    ModelRegistry,
    NodeFragment,
    NodeId,
    count,
    key,
    max_,
    min_,
    path_key,
    refs,
    sum_,
)
from drivine.backend.cypher_grammar import NEO4J_GRAMMAR
from drivine.backend.graph_backend import GraphBackend
from drivine.backend.graph_statement import GraphStatement
from drivine.backend.oracle.oracle_backend import OracleBackend
from drivine.backend.oracle.oracle_graph import OracleGraph
from drivine.backend.page import Page
from drivine.backend.postgres.postgres_backend import PostgresBackend
from drivine.backend.sqlite.sqlite_backend import SqliteBackend
from drivine.backend.sqlite.sqlite_graph import SqliteGraph, text_of
from drivine.dsl.query import Query
from drivine.dsl.query_renderer import QueryRenderer
from drivine.manager.group_operations import GroupOperations


class Scored(NodeFragment, labels=["Scored"]):
    id: Annotated[str, NodeId()]
    name: str | None = None
    score: Annotated[float | None, GraphProperty("$score")] = None


class Unkeyed(NodeFragment, labels=["Unkeyed"]):
    """A node class with no NodeId, which a path key can't order by."""

    name: str | None = None


registry = ModelRegistry()
scored = registry.fragment(Scored)
backend = SqliteBackend(SqliteGraph())
q = refs(Scored)


def labels(type_: type) -> list[str]:
    return registry.fragment(type_).labels


async def grouped(on: GraphBackend = backend, source: type = Scored, **spec: Any) -> GraphStatement:
    """The grouped read `spec` asks of `source` on `on`, as the statement it is written as."""
    statements: list[GraphStatement] = []

    async def query(statement: GraphStatement) -> list[Any]:
        statements.append(statement)
        return []

    await GroupOperations(registry, query, None, on).load(source, **spec)  # type: ignore[arg-type]
    return statements[0]


def test_writes_the_parts_of_a_query_as_the_cypher_it_writes_for_neo4j_each_with_the_values_it_binds():
    query = Query(
        where=[q.name.eq("Ada"), q.score.gt(1)],
        order_by=[q.score.desc(), q.id.asc()],
        seek=[q.score.after(5), q.id.after("s1")],
    )
    parts = QueryRenderer.parts(query, scored, NEO4J_GRAMMAR, "n", labels)
    assert parts.filter is not None and parts.keyset is not None
    assert (parts.filter.cypher, parts.filter.bindings) == (
        "n.name = $_q0 AND n.`$score` > $_q1",
        {"_q0": "Ada", "_q1": 1},
    )
    assert parts.keyset.cypher == (
        "n.`$score` IS NOT NULL AND n.id IS NOT NULL AND n.`$score` <= $_q2 AND "
        "(n.`$score` < $_q2 OR (n.`$score` = $_q2 AND n.id > $_q3))"
    )
    assert parts.keyset.bindings == {"_q2": 5, "_q3": "s1"}
    assert [it.cypher for it in parts.orders] == ["n.`$score`", "n.id"]
    # Rendered whole for a graph engine, as before.
    assert QueryRenderer.render(Query(where=q.name.eq("Ada")), scored, NEO4J_GRAMMAR, "n", labels).where == (
        "n.name = $_q0"
    )


def test_compiles_the_filter_and_the_seek_whole_over_n_and_reads_a_property_whose_name_begins_with_dollar_as_held():
    query = backend.render_query(
        Query(where=q.score.gt(1), order_by=[q.score.desc(), q.id.asc()], seek=[q.score.after(5), q.id.after("s1")]),
        scored,
        labels,
    )
    assert query.where is not None and query.order_by is not None
    assert """n_.props -> '$."$$score"'""" in query.where
    assert """'$."$score"'""" not in query.where
    assert """n_.props -> '$."$$score"'""" in query.order_by
    assert list(query.bindings) == ["_cy__q0", "_cy__q1", "_cy__q2"]
    assert query.labels_tested == ()
    statement = backend.load_all_fragments(scored, query.where, Page(order_by=query.order_by))
    assert "FROM drivine_node n_\nWHERE " in statement.text


def test_places_an_orders_nulls_by_a_term_of_whether_it_is_null_before_it_its_parameters_named_apart():
    query = backend.render_query(Query(order_by=[q.name.asc().nulls_first(), q.id.desc()]), scored, labels)
    assert query.order_by is not None
    # The null term first, descending for nulls first, then the property's terms.
    assert query.order_by.startswith("""(COALESCE(json_type((n_.props -> '$."name"')), 'null') = 'null') DESC""")
    assert query.bindings == {}


def test_names_the_parameters_of_an_order_expression_apart_from_the_filters():
    from drivine import coalesce

    query = backend.render_query(Query(where=q.name.eq("Ada"), order_by=coalesce(q.score, 7).desc()), scored, labels)
    assert query.order_by is not None and "$_cy_f1__q1" in query.order_by
    assert query.bindings == {"_cy__q0": '"Ada"', "_cy_f1__q1": "7"}


async def test_keys_and_adds_a_property_whose_name_begins_with_dollar_as_the_layout_holds_it():
    statement = await grouped(keys={"score": key("$score")}, aggregates={"total": sum_("$score"), "all": count()})
    assert """(n_.props -> '$."$$score"') AS k0""" in statement.text
    assert (
        """SUM((CASE WHEN json_type((n_.props -> '$."$$score"')) IN ('integer', 'real') """
        """THEN (n_.props -> '$."$$score"') ->> '$' END))"""
    ) in statement.text
    assert """'$."$score"'""" not in statement.text


async def test_reads_a_least_or_greatest_value_through_the_node_of_the_group_that_has_it_and_compares_it_there():
    statement = await grouped(
        where=q.name.eq("Ada"),
        keys={"name": key(q.name)},
        aggregates={"low": min_(q.score), "high": max_(q.score, q.score.lt(100)), "all": count()},
        having=[("high", "gte", 3), ("all", "gte", 1)],
        order_by=[("high", "desc")],
    )
    text = statement.text
    # Each is the nid of the group's first node in Cypher's order, of those that have the value and meet the filter.
    assert re.search(
        r"\(json_group_array\(n_\.nid ORDER BY .* NULLS FIRST\) FILTER \(WHERE .*\)\) ->> '\$\[0\]' AS a1", text
    )
    assert "LEFT JOIN drivine_node a0_ ON a0_.nid = g.a0" in text
    assert "LEFT JOIN drivine_node a1_ ON a1_.nid = g.a1" in text
    assert """'a1', json((a1_.props -> '$."$$score"'))""" in text
    # The having of a held value is the module's condition over its node; a count's stays the group's.
    assert re.search(r"\nWHERE .*a1_\.props.*\$_cy_f\d+_value", text)
    assert "HAVING COUNT(*) >= $_having1" in text
    assert re.search(r"\nORDER BY .*a1_\.props", text)
    # The where and the aggregate's filter keep the renderer's names; the library's own fragments are named apart, in
    # the order they are written: the key (f1), each held value's presence and order (f2 to f5), the having (f6).
    assert sorted(statement.bindings) == ["_cy__q0", "_cy__q1", "_cy_f6_value", "_having1"]
    assert statement.bindings["_cy_f6_value"] == "3"


def test_writes_a_patchs_condition_over_n_a_value_in_the_path_of_a_dollar_named_property_left_as_it_is():
    where, bindings = backend.render_where(q.score.eq(2), scored, labels)
    patch = backend.patch_one(scored, where)
    parameters: Any = patch.spec({"id": "s1", "set": {"name": "x"}, "vectors": {}, **bindings, "score": 9}).parameters
    assert "FROM drivine_node n_ WHERE n_.nid = $1 AND (" in parameters["_patchedIf"]
    assert """'$."$$score"'""" in parameters["_patchedIf"]
    assert "$_cy__q0" not in parameters["_patchedIf"]


def test_a_patchs_raw_condition_writes_in_a_value_but_not_a_name_within_a_quoted_text():
    patch = backend.patch_one(scored, """n_.props ->> '$."$$id"' = $id AND n_.props ->> '$.x' = $x""")
    parameters: Any = patch.spec({"id": "s1", "set": {}, "vectors": {}, "x": "it's"}).parameters
    assert """n_.props ->> '$."$$id"' = 's1' AND n_.props ->> '$.x' = 'it''s'""" in parameters["_patchedIf"]


async def test_reads_a_raw_where_over_n_as_the_row_n_a_read_of_the_dsl_calls_its_root_in_a_grouped_reads_too():
    statement = await grouped(
        where="n.name = 'Ada'", keys={"name": key("name")}, aggregates={"named": count("n.props IS NOT NULL")}
    )
    assert """AND ((CASE json_type(n_.props, '$."name"')""" in statement.text
    assert "SUM(CASE WHEN n_.props IS NOT NULL THEN 1 ELSE 0 END)" in statement.text


def test_reads_a_property_written_as_cypher_writes_it_as_the_store_reads_it_over_the_row_a_read_calls_n_():
    assert backend.written("n.title = 'x'") == f"{text_of('n_', 'title')} = 'x'"
    # A column of the row is the row's, which a read of the query DSL calls n_ (see SqliteBackend's ROW_ALIAS).
    assert backend.written("n.props ->> '$.title' = 'x'") == "n_.props ->> '$.title' = 'x'"
    # A property whose name begins with `$` is read by the name the layout holds it by, `$$score`.
    assert backend.written("n.`$score` > 1") == f"{text_of('n_', '$$score')} > 1"


@pytest.mark.parametrize(
    "on", [SqliteBackend(SqliteGraph()), PostgresBackend(), OracleBackend(OracleGraph())], ids=repr
)
async def test_a_grouped_read_keyed_by_a_path_to_a_class_with_no_node_id_is_refused_by_name(on: GraphBackend):
    # drivine-python#539: it was ordered by a property named '', in no set order, without a word.
    with pytest.raises(DrivineError, match=r"^Unkeyed has no NodeId field, so a grouped read can't key by it"):
        await grouped(on, keys={"to": path_key(Unkeyed, [Hop("TO")])}, aggregates={"all": count()})
