"""How Oracle's node rows are tested for labels: the first through the index over labels, the rest by their text."""

from drivine.backend.oracle.oracle_graph import has_labels


def test_tests_the_first_label_through_the_index_over_labels_and_the_rest_by_their_text_as_drivine4j_does():
    assert has_labels("n", ["A"]) == "JSON_EXISTS(n.labels, '$?(@ == \"A\")')"
    assert has_labels("n", ["A", "B", 'it\'s "q"']) == (
        "JSON_EXISTS(n.labels, '$?(@ == \"A\")') AND INSTR(JSON_SERIALIZE(n.labels), '\"B\"') > 0 AND "
        "INSTR(JSON_SERIALIZE(n.labels), '\"it''s \\\"q\\\"\"') > 0"
    )


def test_escapes_a_label_from_data_for_the_json_path_and_the_sql_literal():
    assert has_labels("n", ["x\"y'z"]) == "JSON_EXISTS(n.labels, '$?(@ == \"x\\\"y''z\")')"
    assert has_labels("n", ['x\\") || 1']) == 'JSON_EXISTS(n.labels, \'$?(@ == "x\\\\\\") || 1")\')'


def test_escapes_a_label_after_the_first_alike_in_the_text_it_is_found_in():
    assert has_labels("n", ["A", "x\\\"') > 0 OR 1 = 1 --"]) == (
        "JSON_EXISTS(n.labels, '$?(@ == \"A\")') AND "
        "INSTR(JSON_SERIALIZE(n.labels), '\"x\\\\\\\"'') > 0 OR 1 = 1 --\"') > 0"
    )
