"""
drivine4j's CypherShorthandTest, ported as TypeScript's Drivine ported it: a predicate written by hand, with the root's
properties written as Cypher writes them, read on Oracle, as drivine4j reads it.
"""

from drivine.backend.cypher_backend import CypherBackend
from drivine.backend.cypher_grammar import NEO4J_GRAMMAR
from drivine.backend.oracle.oracle_backend import OracleBackend
from drivine.backend.oracle.oracle_graph import text_of as oracle
from drivine.backend.postgres.postgres_backend import PostgresBackend
from drivine.backend.sql.cypher_shorthand import CypherShorthand

shorthand = CypherShorthand("n", "n_", oracle)


def rewrite(predicate: str) -> str:
    return shorthand.rewrite(predicate)


def test_a_property_of_the_root_is_read_from_its_document():
    assert rewrite("n.name = 'x'") == "JSON_VALUE(n_.props, '$.name') = 'x'"


def test_a_name_in_backticks_is_one_key_quoted_in_the_path():
    assert rewrite("n.`a.b` = 1") == "JSON_VALUE(n_.props, '$.\"a.b\"') = 1"


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
    assert OracleBackend().written("n.id = 'c1' AND people.name = 'x'") == (
        "JSON_VALUE(n.props, '$.id') = 'c1' AND people.name = 'x'"
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
    assert PostgresBackend().written("n.name = 'x' AND n.locked = true AND n.stamp IS NULL") == (
        "(n.props ->> 'name') = 'x' AND (n.props ->> 'locked') = 'true' AND n.stamp IS NULL"
    )
    assert CypherBackend(NEO4J_GRAMMAR).written("n.name = 'x'") == "n.name = 'x'"
