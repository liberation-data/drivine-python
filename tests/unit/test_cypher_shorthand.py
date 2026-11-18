"""
drivine4j's CypherShorthandTest, ported as TypeScript's Drivine ported it: a predicate written by hand, with the root's
properties written as Cypher writes them, read on Oracle, as drivine4j reads it.
"""

from collections.abc import Mapping
from typing import Any

from drivine.backend.cypher_backend import CypherBackend
from drivine.backend.cypher_grammar import NEO4J_GRAMMAR
from drivine.backend.oracle.oracle_backend import OracleBackend
from drivine.backend.oracle.oracle_graph import text_of as oracle
from drivine.backend.postgres.postgres_backend import PostgresBackend
from drivine.backend.sql.cypher_shorthand import CypherShorthand
from drivine.backend.sqlite.sqlite_backend import SqliteBackend

shorthand = CypherShorthand("n", "n_", oracle)


def rewrite(predicate: str, bindings: Mapping[str, Any] | None = None) -> str:
    return shorthand.rewrite(predicate, bindings)


def test_a_property_of_the_root_is_read_from_its_document():
    assert rewrite("n.name = 'x'") == "JSON_VALUE(n_.props, '$.name') = 'x'"


def test_a_name_in_backticks_is_one_key_quoted_in_the_path():
    assert rewrite("n.`a.b` = 1") == "JSON_VALUE(n_.props, '$.\"a.b\"') = 1"


def test_a_property_whose_name_begins_with_dollar_is_read_by_the_key_the_layout_holds_it_under():
    # drivine-python#543, a port of liberation-data/drivine#793: the `$` doubled, as drivine4j reads it.
    assert rewrite("n.`$score` > 1") == "JSON_VALUE(n_.props, '$.\"$$score\"') > 1"
    assert rewrite("n.`$open` = true") == "JSON_VALUE(n_.props, '$.\"$$open\"') = 'true'"
    assert rewrite("n.`a$b` = 'x'") == "JSON_VALUE(n_.props, '$.\"a$b\"') = 'x'"
    # Each SQL store's raw where reads it so.
    assert OracleBackend().written("n.`$score` > 1") == "JSON_VALUE(n_.props, '$.\"$$score\"') > 1"
    assert PostgresBackend().written("n.`$score` > 1") == "(n_.props ->> '$$score') > 1"
    assert SqliteBackend().written("n.`$score` > 1") == (
        "(CASE json_type(n_.props, '$.\"$$score\"') WHEN 'text' THEN n_.props ->> '$.\"$$score\"' "
        "WHEN 'null' THEN NULL ELSE n_.props -> '$.\"$$score\"' END) > 1"
    )


def test_the_rows_own_columns_are_the_columns():
    assert rewrite("n.stamp IS NULL") == "n_.stamp IS NULL"
    assert rewrite("JSON_EXISTS(n.props, '$.x')") == "JSON_EXISTS(n_.props, '$.x')"
    assert rewrite("JSON_SERIALIZE(n.labels)") == "JSON_SERIALIZE(n_.labels)"
    assert rewrite("n.nid > 0") == "n_.nid > 0"


def test_text_inside_a_string_literal_is_left_as_it_is():
    assert rewrite("n.name = 'n.name'") == "JSON_VALUE(n_.props, '$.name') = 'n.name'"
    assert rewrite("n.text = 'it''s n.text, see'") == "JSON_VALUE(n_.props, '$.text') = 'it''s n.text, see'"
    assert rewrite("'n.name'") == "'n.name'"


def test_another_alias_is_not_the_root():
    assert rewrite("other.name = 'x'") == "other.name = 'x'"
    assert rewrite("an.name = 'x'") == "an.name = 'x'"
    assert rewrite("x.n.name = 'x'") == "x.n.name = 'x'"


def test_a_property_inside_a_function_is_rewritten_a_function_on_the_alias_is_not():
    assert rewrite("LOWER(n.x) = 'a'") == "LOWER(JSON_VALUE(n_.props, '$.x')) = 'a'"
    assert rewrite("n.fn(1) = 2") == "n.fn(1) = 2"


def test_several_properties_in_one_predicate_are_each_rewritten():
    assert rewrite("n.a = n.b AND n.c IS NOT NULL") == (
        "JSON_VALUE(n_.props, '$.a') = JSON_VALUE(n_.props, '$.b') AND JSON_VALUE(n_.props, '$.c') IS NOT NULL"
    )


def test_a_views_root_is_read_by_what_the_predicate_calls_it_and_no_related_nodes():
    assert (
        CypherShorthand("claim", "claim_", oracle).rewrite("claim.id = 'c1'")
        == "JSON_VALUE(claim_.props, '$.id') = 'c1'"
    )
    # Over `n_`, the row a read of the query DSL calls its root.
    assert OracleBackend().written("n.id = 'c1' AND people.name = 'x'") == (
        "JSON_VALUE(n_.props, '$.id') = 'c1' AND people.name = 'x'"
    )


def test_a_predicate_with_nothing_of_the_root_in_it_is_unchanged():
    assert rewrite("1 = 1") == "1 = 1"
    assert rewrite("") == ""


def test_a_property_compared_with_a_boolean_is_compared_with_the_text_a_json_boolean_is_read_as():
    assert rewrite("n.locked = true") == "JSON_VALUE(n_.props, '$.locked') = 'true'"
    assert rewrite("n.locked <> FALSE AND n.stamp IS NOT NULL") == (
        "JSON_VALUE(n_.props, '$.locked') <> 'false' AND n_.stamp IS NOT NULL"
    )
    # A word that only begins as a boolean is not one, and nor is a boolean compared otherwise.
    assert rewrite("n.state = truest") == "JSON_VALUE(n_.props, '$.state') = truest"
    assert rewrite("true = n.locked") == "true = JSON_VALUE(n_.props, '$.locked')"


def test_postgres_reads_a_property_as_it_reads_one_and_a_graph_engine_keeps_cypher():
    # Over `n_`, the row a read of the query DSL calls its root.
    assert PostgresBackend().written("n.name = 'x' AND n.locked = true AND n.stamp IS NULL") == (
        "(n_.props ->> 'name') = 'x' AND (n_.props ->> 'locked') = 'true' AND n_.stamp IS NULL"
    )
    assert CypherBackend(NEO4J_GRAMMAR).written("n.name = 'x'") == "n.name = 'x'"


def test_a_quote_in_a_name_in_backticks_stays_inside_the_literal_it_is_written_in():
    assert rewrite("n.`it's` = 1") == "JSON_VALUE(n_.props, '$.\"it''s\"') = 1"
    assert rewrite('n.`a"b\\c` = 1') == 'JSON_VALUE(n_.props, \'$."a\\"b\\\\c"\') = 1'
    assert rewrite("n.`a\n` = 1") == "JSON_VALUE(n_.props, '$.\"a\n\"') = 1"
    assert PostgresBackend().written("n.`it's` = 1") == "(n_.props ->> 'it''s') = 1"


def test_a_property_compared_with_a_parameter_bound_to_a_boolean_is_compared_with_the_booleans_text():
    # drivine-python#542: a driver binds a boolean as no text a property is read as (SQLite's as 1), so the comparison
    # would hold for no node; it is compared as `n.open = true` is.
    bound = {"open": True, "shut": False, "count": 1, "name": "x"}
    assert rewrite("n.open = $open AND n.shut <> $shut", bound) == (
        "JSON_VALUE(n_.props, '$.open') = 'true' AND JSON_VALUE(n_.props, '$.shut') <> 'false'"
    )
    # A parameter bound to anything else, one bound to none, and a longer name are read as they are.
    assert rewrite("n.count = $count AND n.name = $name AND n.open = $missing", bound) == (
        "JSON_VALUE(n_.props, '$.count') = $count AND JSON_VALUE(n_.props, '$.name') = $name AND "
        "JSON_VALUE(n_.props, '$.open') = $missing"
    )
    assert rewrite("n.open = $opened", {"open": True, "opened": 1}) == "JSON_VALUE(n_.props, '$.open') = $opened"
    # SQL of the caller's own over the row reads the boolean as the driver binds it.
    assert SqliteBackend().written("(n.props ->> '$.open') = $open", bound) == "(n_.props ->> '$.open') = $open"
    # Without the bindings the predicate is read as before.
    assert rewrite("n.open = $open") == "JSON_VALUE(n_.props, '$.open') = $open"
