"""How Oracle's node rows are tested for labels: the first through the index over labels, the rest by their text."""

from drivine.backend.oracle.oracle_graph import has_labels, holds_label, serialized_label
from drivine.cypher.sql.oracle_cypher_compiler import OracleCypherCompiler

HELD = "INSTR(',' || LTRIM(RTRIM(JSON_SERIALIZE(n.labels), ']'), '[') || ',\"',"
"""The start of the test of a later label, up to its needle."""


def test_tests_the_first_label_through_the_index_over_labels_and_the_rest_by_their_text_as_drivine4j_does():
    assert has_labels("n", ["A"]) == "JSON_EXISTS(n.labels, '$?(@ == \"A\")')"
    assert has_labels("n", ["A", "B", 'it\'s "q"']) == (
        f'JSON_EXISTS(n.labels, \'$?(@ == "A")\') AND {HELD} \',"B","\') > 0 AND {HELD} \',"it\'\'s \\"q\\"","\') > 0'
    )


def test_looks_for_a_later_label_as_one_whole_label_among_the_labels_made_commas_at_each_end():
    assert holds_label("c.labels", 'x","Dog') == (
        "INSTR(',' || LTRIM(RTRIM(JSON_SERIALIZE(c.labels), ']'), '[') || ',\"', ',\"x\\\",\\\"Dog\",\"') > 0"
    )
    assert (
        has_labels("n", ["A", "Dog"]) == f"JSON_EXISTS(n.labels, '$?(@ == \"A\")') AND {holds_label('n.labels', 'Dog')}"
    )


def test_writes_a_label_as_oracles_json_serialize_writes_it_control_characters_and_line_separators_escaped():
    assert serialized_label("plain \u00e9 \U0001f600 / '") == '"plain \u00e9 \U0001f600 / \'"'
    assert serialized_label('"\\') == '"\\"\\\\"'
    assert serialized_label("\b\t\n\f\r") == '"\\b\\t\\n\\f\\r"'
    assert serialized_label("\u0000\u0001\u000b\u001f\u007f") == '"\\u0000\\u0001\\u000B\\u001F\\u007F"'
    assert serialized_label("\u2028\u2029\u0080\u2027\u202a") == '"\\u2028\\u2029\u0080\u2027\u202a"'


def test_the_cypher_compiler_tests_a_later_label_as_the_backend_does():
    sql = OracleCypherCompiler("g").compile('MATCH (n:Crafted:`x","Dog`:`a\tb`) RETURN n.id', {}).sql
    assert "JSON_EXISTS(n1.labels, '$[*]?(@ == \"Crafted\")')" in sql
    assert holds_label("n1.labels", 'x","Dog') in sql
    assert "INSTR(',' || LTRIM(RTRIM(JSON_SERIALIZE(n1.labels), ']'), '[') || ',\"', ',\"a\\tb\",\"') > 0" in sql


def test_escapes_a_label_from_data_for_the_json_path_and_the_sql_literal():
    assert has_labels("n", ["x\"y'z"]) == "JSON_EXISTS(n.labels, '$?(@ == \"x\\\"y''z\")')"
    assert has_labels("n", ['x\\") || 1']) == 'JSON_EXISTS(n.labels, \'$?(@ == "x\\\\\\") || 1")\')'


def test_escapes_a_label_after_the_first_alike_in_the_text_it_is_found_in():
    assert has_labels("n", ["A", "x\\\"') > 0 OR 1 = 1 --"]) == (
        f"JSON_EXISTS(n.labels, '$?(@ == \"A\")') AND {HELD} ',\"x\\\\\\\"'') > 0 OR 1 = 1 --\",\"') > 0"
    )
