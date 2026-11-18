"""
The query DSL on Oracle as the shared module's fragments (drivine-cypher-spec#235, step 4), as on SQLite and Postgres:
the Cypher the DSL writes for Neo4j, its filter and its seek each compiled whole over the row `n_`, each order, group
key and aggregate the library writes a fragment named apart; with what Oracle's fragments ask of the statement written
in, drivine4j's OracleSelect: their hints after its first SELECT or DELETE, a node joined back by the keys Oracle keeps,
the index-key binds beside the values', and the stored functions they call said. Pinned here as SQL; the Oracle
integration tests run them. Drivine for TypeScript's OracleQueryDsl.spec of liberation-data/drivine#797.
"""

from __future__ import annotations

import re
import uuid
from typing import Annotated, Any

import pytest
from pydantic import BaseModel

from drivine import (
    CascadeType,
    DatabaseType,
    DrivineError,
    GraphProperty,
    GraphView,
    ModelRegistry,
    NodeFragment,
    NodeId,
    PathKey,
    QuerySpecification,
    Root,
    any_of,
    count,
    key,
    max_,
    min_,
    refs,
    sum_,
    times,
)
from drivine.annotation.hop import Hop
from drivine.backend.graph_statement import GraphStatement, StoredFunctionCalls
from drivine.backend.matching_search import MatchingSearch
from drivine.backend.nearest_search import NearestSearch
from drivine.backend.oracle.oracle_backend import OracleBackend
from drivine.backend.oracle.oracle_graph import OracleGraph
from drivine.backend.page import Page
from drivine.backend.sql.fragments import same_key
from drivine.connection.oracle.oracle_spec_compiler import OracleSpecCompiler
from drivine.dsl.lower_copy_lookups import lowered_where
from drivine.dsl.query import Query
from drivine.manager.group_query import GroupQuery
from drivine.schema.planned_managers import PlannedConstraintManager, PlannedIndexManager
from drivine.schema.schema_grammar import schema_grammar_for
from drivine.schema.schema_item import SchemaItem
from drivine.schema.schema_specs import range_index, uniqueness_constraint
from tests.models import Account, LocationHierarchy
from tests.unit.test_schema_plan import Host, planning


class Scored(NodeFragment, labels=["Scored"]):
    id: Annotated[str, NodeId()]
    name: str | None = None
    score: Annotated[float | None, GraphProperty("$score")] = None
    meta: dict[str, Any] | None = None


class ScoredView(GraphView):
    scored: Annotated[Scored, Root()]


class Judge(NodeFragment, labels=["Judge"]):
    code: Annotated[str, NodeId()]


registry = ModelRegistry()
scored = registry.fragment(Scored)
view = registry.view(ScoredView)
hierarchy = registry.view(LocationHierarchy)
account = registry.fragment(Account)
backend = OracleBackend(OracleGraph())
q = refs(Scored)

HINT = "/*+ NO_JSON_TABLE_TRANSFORM */"
"""The hint the module's fragments on Oracle ask the statement they are in to carry."""

INDEXED = [SchemaItem("Scored", ("name",))]


def labels(it: type) -> list[str]:
    return registry.fragment(it).labels


def rendered(query: Query, target: Any = scored, indexed: list[SchemaItem] | None = None) -> Any:
    return backend.render_query(query, target, labels, indexed or [])


def where_of(where: Any, target: Any = scored) -> str:
    return backend.render_where(where, target, labels)[0]


def grouped(query: GroupQuery, targets: dict[str, Any] | None = None) -> GraphStatement:
    """The grouped read of Scored on Oracle, a raw where written as the manager writes it (see GroupOperations)."""
    if isinstance(query.where, str):
        query = GroupQuery(**{**query.__dict__, "where": backend.written(query.where)})
    return backend.groups(scored, query, targets or {}, labels)


def named(text: str) -> list[str]:
    """Every `$name` a statement's text names as a parameter: not within a quoted text, as a JSON path's `'$.x'` is."""
    return re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", re.sub(r"'(?:[^']|'')*'", "''", text))


NULLS_EQUAL = StoredFunctionCalls("drivine", nulls_equal=True)
"""What a statement that compares a map holding a null calls: the graph's nulls_equal."""


def test_the_filter_and_the_seek_are_compiled_whole_over_n_and_a_dollar_named_property_read_as_held():
    query = rendered(
        Query(where=q.score.gt(1), order_by=[q.score.desc(), q.id.asc()], seek=[q.score.after(5), q.id.after("s1")])
    )
    assert 'n_.props, \'$."$$score"' in query.where
    assert '\'$."$score"' not in query.where
    assert 'n_.props, \'$."$$score"' in query.order_by
    assert query.labels_tested == ()
    statement = backend.load_all_fragments(scored, query.where, Page(order_by=query.order_by))
    assert "FROM drivine_node n_\nWHERE " in statement.text


def test_the_hint_is_written_once_after_a_loads_first_select_and_no_note_of_it_is_left():
    query = rendered(Query(where=[q.name.eq("Ada"), q.score.gt(1)], order_by=[q.name.asc()]))
    statement = backend.load_all_fragments(scored, query.where, Page(order_by=query.order_by))
    assert statement.text.startswith(f"SELECT {HINT} JSON_OBJECT(")
    assert statement.text.count(HINT) == 1
    assert "drivine:" not in statement.text
    assert backend.count_fragments(scored, query.where).text.startswith(f"SELECT {HINT} COUNT(*)")
    # A filter of a view's root, and a search's filter, are hinted in the statement too.
    views = rendered(Query(where=refs(ScoredView).scored.name.eq("Ada")), view)
    assert backend.load_all_views(view, views.where).text.startswith(f"SELECT {HINT} ")
    nearest = backend.nearest(scored, NearestSearch("Scored", "e", "COSINE", 3, where_of(q.name.eq("Ada"))))
    assert nearest.text.startswith(f"SELECT {HINT} JSON_OBJECT('value'")
    assert "JOIN drivine_node n_ ON (n_.nid = s.nid + 0 AND s.nid = n_.nid + 0)" in nearest.text


def test_the_hint_is_written_after_a_deletes_delete_and_in_a_block_on_the_statement_the_filter_is_in():
    where = where_of(q.name.eq("Ada"))
    delete_all = backend.delete_all(scored, where).text
    assert f"    DELETE {HINT} FROM drivine_node n_\nWHERE " in delete_all
    assert f"SELECT {HINT}" not in delete_all
    assert f"    DELETE {HINT} FROM drivine_node n_\nWHERE " in backend.delete(scored, where).text
    # A patch's condition: its own SELECT in the block, over n_, and not the block's first.
    patch = backend.patch_one(scored, where).text
    assert f"SELECT {HINT} COUNT(*) INTO v_meets FROM drivine_node n_ WHERE n_.nid = v_root AND (" in patch
    assert patch.count(HINT) == 1
    assert "drivine:" not in patch
    # A statement that holds no fragment carries no hint.
    assert "/*+" not in backend.delete_all(scored, backend.written("n.name = 'Ada'")).text


def test_a_cascades_roots_and_a_depth_checks_are_hinted_on_the_statement_of_the_block_that_reads_the_filter():
    where = where_of(refs(LocationHierarchy).location.name.eq("Ada"), hierarchy)
    cascade = backend.delete(hierarchy, where, CascadeType.DELETE_ALL).text
    assert f"    SELECT {HINT} n_.nid BULK COLLECT INTO v_roots FROM drivine_node n_\nWHERE " in cascade
    assert cascade.count(HINT) == 1
    assert "drivine:" not in cascade
    check = backend.cascade_depth_checks(hierarchy, where)[0].statement.text
    assert f"WITH cascade_roots AS (SELECT {HINT} n_.nid FROM drivine_node n_\nWHERE " in check
    assert check.count(HINT) == 1
    assert "drivine:" not in check


def test_an_equalitys_index_key_is_bound_as_plain_text_and_every_name_bound_is_in_its_sql():
    query = rendered(Query(where=[q.name.eq("Ada"), q.score.gt(1)], order_by=[q.name.asc()], seek=q.name.after("B")))
    assert query.bindings["_cyk__q0"] == "Ada"
    assert "$_cyk__q0" in query.where
    statement = backend.load_all_fragments(scored, query.where, Page(order_by=query.order_by))
    assert set(named(statement.text)) == set(query.bindings)
    # Bound as it is, a VARCHAR2: the SQL compares the index's columns with it as text, never as JSON or a CLOB.
    compiled = OracleSpecCompiler(statement.spec(query.bindings).finalized_copy("SQL")).compile()
    assert "Ada" in compiled.parameters.values()


def test_an_equality_is_read_through_the_labelled_index_the_module_writes_and_the_label_not_tested_again():
    query = rendered(Query(where=[q.name.eq("Ada"), q.score.gt(1)]), indexed=INDEXED)
    assert "CASE WHEN JSON_EXISTS(n_.labels, '$[*]?(@ == \"Scored\")') THEN" in query.where
    assert query.labels_tested == ("Scored",)
    statement = backend.load_all_fragments(scored, query.where, None, query.labels_tested)
    assert "JSON_EXISTS(n_.labels, '$?(@ == \"Scored\")')" not in statement.text
    # Inside an or the module reads no index, and the label is tested as before.
    assert rendered(Query(where=any_of(q.name.eq("Ada"), q.name.eq("Bo"))), indexed=INDEXED).labels_tested == ()
    # A range comparison is not read through the index: the module reads an equality alone through it.
    assert rendered(Query(where=q.name.gte("A")), indexed=INDEXED).labels_tested == ()


def test_a_views_root_equality_implies_its_label_and_a_related_nodes_of_the_same_label_does_not():
    h = refs(LocationHierarchy)
    indexed = [SchemaItem("Location", ("name",))]
    root = rendered(Query(where=h.location.name.eq("Ada")), hierarchy, indexed)
    assert root.labels_tested == ("Location",)
    tested = "JSON_EXISTS(n_.labels, '$?(@ == \"Location\")')"
    assert tested not in backend.load_all_views(hierarchy, root.where, None, None, root.labels_tested).text
    assert tested not in backend.count_views(hierarchy, root.where, None, root.labels_tested).text
    # A sub-location's equality is the related node's, read over its own row: the root's label is still tested.
    related = rendered(Query(where=h.sub_locations.any(lambda it: it.location.name.eq("Ada"))), hierarchy, indexed)
    assert related.labels_tested == ()
    assert tested in backend.load_all_views(hierarchy, related.where, None, None, related.labels_tested).text


def lowered(where: Any, indexed: list[SchemaItem]) -> Any:
    return rendered(Query(where=lowered_where(where, account, {"email"})), account, indexed)


def test_the_label_of_an_ignore_case_equality_read_through_the_index_over_a_lowercased_copy_is_tested():
    # drivine-cypher-spec#373: the module reads the copy through the labelled index, but past 4,000 bytes its term
    # holds for any node, so the statement tests the label itself.
    a = refs(Account)
    copy = [SchemaItem("Account", ("__drivine.lower.email",))]
    by_name = [*copy, SchemaItem("Account", ("name",))]
    query = lowered(a.email.eq_ignore_case("Ada@X.com"), copy)
    assert "CASE WHEN JSON_EXISTS(n_.labels, '$[*]?(@ == \"Account\")') THEN" in query.where
    assert query.labels_tested == ()
    statement = backend.load_all_fragments(account, query.where, None, query.labels_tested)
    assert "WHERE JSON_EXISTS(n_.labels, '$?(@ == \"Account\")') AND (" in statement.text
    # An equality of the copy's own value, not lowered, still tests the label through the index.
    plain = rendered(Query(where=a.property("__drivine.lower.email").eq("ada@x.com")), account, copy)
    assert plain.labels_tested == ("Account",)
    # As does a plain indexed equality beside the lowered one.
    assert lowered([a.email.eq_ignore_case("Ada@X.com"), a.name.eq("Ada")], by_name).labels_tested == ("Account",)
    # But not an equality of an indexed property the module reads through no index, a list's or "": its term holds no
    # label test, and the lowered one's doesn't decide it.
    for value in (["Ada"], ""):
        unread = lowered([a.email.eq_ignore_case("Ada@X.com"), a.name.eq(value)], by_name)
        assert "CASE WHEN JSON_EXISTS(n_.labels, '$[*]?(@ == \"Account\")') THEN" in unread.where
        assert unread.labels_tested == ()


def test_a_text_past_4000_bytes_is_compared_as_a_clob_not_through_the_index_and_the_label_tested():
    # 4,000 bytes is read through the index; one byte more, or 2,001 two-byte characters, is not.
    at = rendered(Query(where=q.name.eq("a" * 4000)), indexed=INDEXED)
    assert at.labels_tested == ("Scored",)
    assert list(at.bindings) == ["_cyk__q0"]
    for long in ("a" * 4001, "é" * 2001):
        query = rendered(Query(where=q.name.eq(long)), indexed=INDEXED)
        assert "CASE WHEN JSON_EXISTS(n_.labels, '$[*]?(@ == \"Scored\")') THEN" not in query.where
        assert "DBMS_LOB.COMPARE(TO_CLOB(JSON_VALUE(n_.props, '$.name' RETURNING CLOB))" in query.where
        assert query.labels_tested == ()
        assert set(query.bindings) == {"_cy__q0"}
        statement = backend.load_all_fragments(scored, query.where, None, query.labels_tested)
        assert "WHERE JSON_EXISTS(n_.labels, '$?(@ == \"Scored\")') AND (" in statement.text


def columns(ran: list[str], kind: str) -> list[str]:
    """The expressions the index of the kind (lx2, uq2) is made over, over the DSL's row, split at the top level."""
    made = re.search(rf"INDEX IF NOT EXISTS drivine_{kind}_Scoredname_\w+ ON drivine_node n \((.*)\)';", "\n".join(ran))
    assert made is not None, ran
    ddl = re.sub(r"\bn\.", "n_.", made.group(1).replace("''", "'"))
    split: list[str] = []
    depth = start = 0
    for i, c in enumerate(ddl):
        depth += 1 if c == "(" else -1 if c == ")" else 0
        if c == "," and depth == 0:
            split.append(ddl[start:i].strip())
            start = i + 1
    return [*split, ddl[start:].strip()]


async def test_an_indexed_equality_is_written_over_the_expressions_of_layout_2s_labelled_and_unique_indexes():
    host = Host()
    grammar = schema_grammar_for(DatabaseType.ORACLE, OracleGraph())
    await PlannedIndexManager(planning("oracle", host), grammar).ensure(range_index("Scored", "name"))
    await PlannedConstraintManager(planning("oracle", host), grammar).ensure(uniqueness_constraint("Scored", "name"))
    where = rendered(Query(where=q.name.eq("Ada")), indexed=INDEXED).where
    for kind in ("lx2", "uq2"):
        type_, value = columns(host.ran, kind)[:2]
        assert f"{type_} = 'string'" in where
        assert f"{value} = $_cyk__q0" in where


def test_a_statement_says_which_stored_functions_it_calls_for_the_connection_to_make_sure_of_them():
    where = where_of(q.meta.eq({"a": None}))
    assert "drivine_nulls_equal(" in where
    for statement in (
        backend.load_all_fragments(scored, where),
        backend.count_fragments(scored, where),
        backend.delete_all(scored, where),
        backend.patch_one(scored, where),
    ):
        assert statement.calls == NULLS_EQUAL
        assert "drivine:" not in statement.text
        assert statement.spec().stored_functions == NULLS_EQUAL
        assert statement.spec().finalized_copy("SQL").stored_functions == NULLS_EQUAL
    assert backend.count_fragments(scored, where_of(q.name.eq("x"))).calls is None
    assert QuerySpecification[Any]("SELECT 1 FROM dual").stored_functions is None


def test_a_searchs_a_cascades_and_a_grouped_reads_filter_say_the_stored_functions_they_call():
    where = where_of(q.meta.eq({"a": None}))
    rooted = where_of(refs(LocationHierarchy).location.name.eq({"a": None}), hierarchy)
    for statement in (
        backend.nearest(scored, NearestSearch("Scored", "e", "COSINE", 3, where)),
        backend.matching(scored, MatchingSearch("Scored", ("name",), 3, where, text="ada")),
        backend.delete(hierarchy, rooted, CascadeType.DELETE_ALL),
        backend.cascade_depth_checks(hierarchy, rooted)[0].statement,
        grouped(GroupQuery({"name": key(q.name)}, where=q.meta.eq({"a": None}))),
    ):
        assert statement.calls == NULLS_EQUAL, statement.text
        assert "drivine:" not in statement.text
        assert statement.spec().stored_functions == NULLS_EQUAL


def test_a_statement_says_it_calls_the_graphs_temporal_arithmetic_where_an_order_by_arithmetic_of_a_property_does():
    # A property's value may be a date, so the module works out the arithmetic by the graph's function.
    query = rendered(Query(order_by=times(q.score, 2).desc()))
    assert "drivine_temporal_arith(" in query.order_by
    statement = backend.load_all_fragments(scored, query.where, Page(order_by=query.order_by))
    assert statement.calls == StoredFunctionCalls("drivine", temporal_arithmetic=True)
    assert "drivine:" not in statement.text


def test_a_regular_expression_is_read_as_the_module_does_and_its_refusal_of_one_it_cannot_answer_taken():
    assert "REGEXP_LIKE(" in where_of(q.name.matches("(?i)(a).*\\1"))
    with pytest.raises(DrivineError, match=r"backreference \\9"):
        where_of(q.name.matches("(a)(b)(c)(d)(e)(f)(g)(h)(i)\\9"))


def test_a_count_and_a_delete_are_over_n_and_a_raw_where_over_n_is_read_as_n_():
    where = backend.written("n.name = 'Ada'")
    assert where == "JSON_VALUE(n_.props, '$.name') = 'Ada'"
    assert "FROM drivine_node n_\nWHERE " in backend.count_fragments(scored, where).text
    assert "DELETE FROM drivine_node n_\nWHERE " in backend.delete_all(scored, where).text
    # A write's own reads keep the row `n`.
    assert "FROM drivine_node n\nWHERE " in backend.held_properties(scored).text
    assert "FROM drivine_node n\nWHERE " in backend.found_stamp(scored).text


def test_a_dollar_named_property_is_keyed_and_added_as_the_layout_holds_it():
    statement = grouped(GroupQuery({"score": key("$score")}, {"total": sum_("$score"), "all": count()}))
    assert 'n_.props, \'$?(@."$$score".type()' in statement.text
    assert "SUM(JSON_VALUE(n_.props, '$.\"$$score\"' RETURNING NUMBER))" in statement.text
    assert '\'$."$score"' not in statement.text
    # A property key is ordered through the group's node of least nid, joined back by the keys Oracle keeps.
    assert "MIN(n_.nid) AS k0r" in statement.text
    assert f"LEFT JOIN drivine_node k0_ ON {same_key('k0_.nid', 'g.k0r')}" in statement.text
    assert f"JOIN drivine_node n_ ON {same_key('n_.nid', 'm.nid')}" in statement.text
    assert statement.text.startswith(f"SELECT {HINT} JSON_OBJECT(")
    assert "FIRST_VALUE" not in statement.text


def test_a_least_or_greatest_values_node_is_taken_by_a_window_over_the_rows_before_they_are_grouped():
    statement = grouped(
        GroupQuery(
            {"name": key(q.name)},
            {"low": min_(q.score), "high": max_(q.score, q.score.lt(100)), "all": count()},
            where=q.name.eq("Ada"),
            having=[("high", "gte", 3), ("all", "gte", 1)],
            order_by=[("high", "desc")],
        )
    )
    text = statement.text
    # Each the first of its group by a window, those that have one and meet the filter first; null for none.
    assert re.search(
        r"FIRST_VALUE\(n_\.nid\) OVER \(PARTITION BY m\.k0 ORDER BY CASE WHEN .* THEN 0 ELSE 1 END, .*\) AS w0", text
    )
    assert re.search(r"FIRST_VALUE\(n_\.nid\) OVER \(PARTITION BY m\.k0 ORDER BY .* DESC\) AS w1", text)
    assert re.search(r"CASE WHEN COUNT\(CASE WHEN .* THEN 1 END\) > 0 THEN MIN\(n_\.w0\) END AS a0, ", text)
    assert "SELECT m.k0, n_.*, FIRST_VALUE(" in text
    assert "    ) n_\n    GROUP BY n_.k0" in text
    # Not KEEP (DENSE_RANK FIRST …), which ends Oracle's server process ordered by a value of JSON.
    assert "DENSE_RANK" not in text
    for holder in ("a0_", "a1_"):
        assert f"LEFT JOIN drivine_node {holder} ON {same_key(f'{holder}.nid', f'g.{holder[:2]}')}" in text
    assert re.search(r"\nWHERE .*a1_\.props.*\$_cy_f\d+_value", text)
    assert "HAVING COUNT(*) >= $_having1" in text
    assert re.search(r"\nORDER BY .*a1_\.props", text)
    assert text.count(HINT) == 1
    assert "_cyk__q1" in statement.bindings
    assert set(named(text)) <= set(statement.bindings)


def test_a_path_key_is_ordered_through_its_node_joined_back_by_the_nodes_id_as_cypher_orders_it():
    statement = grouped(
        GroupQuery({"judge": PathKey(Judge, [Hop("JUDGED_BY")])}, {"all": count()}),
        {"judge": registry.fragment(Judge)},
    )
    assert f"LEFT JOIN drivine_node k0_ ON {same_key('k0_.nid', 'g.k0')}" in statement.text
    assert re.search(r"\nORDER BY .*k0_\.props, '\$\.code", statement.text)


def test_a_raw_where_over_n_is_read_as_the_row_n_in_a_grouped_reads_too():
    statement = grouped(
        GroupQuery({"name": key("name")}, {"named": count(backend.written("n.props IS NOT NULL"))}, "n.name = 'Ada'")
    )
    assert "AND (JSON_VALUE(n_.props, '$.name') = 'Ada'))" in statement.text
    assert "SUM(CASE WHEN n_.props IS NOT NULL THEN 1 ELSE 0 END)" in statement.text


class Place(BaseModel):
    city: str


class Keyed(NodeFragment, labels=["Keyed"]):
    id: Annotated[uuid.UUID, NodeId()]
    place: Place | None = None


def test_a_uuid_and_a_model_are_bound_as_the_json_a_save_holds_them_as_in_a_condition_and_a_seek():
    # A save holds a UUID as its text and a model as the JSON pydantic writes it (value_layout.write), which the
    # module's parameters can't take as they are (drivine-python#547).
    k = refs(Keyed)
    one = uuid.UUID(int=1)
    query = backend.render_query(
        Query(where=[k.id.in_([one]), k.place.eq(Place(city="Oslo"))], order_by=[k.id.asc()], seek=[k.id.after(one)]),
        registry.fragment(Keyed),
        labels,
    )
    assert query.bindings == {"_cy__q0": f'["{one}"]', "_cy__q1": '{"city":"Oslo"}', "_cy__q2": f'"{one}"'}
