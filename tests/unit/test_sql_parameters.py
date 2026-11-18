"""
The `$name` parameters of a SQL statement, outside its quoted text. A port of Drivine for TypeScript's
SqlParameters.spec.
"""

from drivine.query.sql_parameters import parameter_names, replace_parameters


def marked(sql: str) -> str:
    """`sql` with each parameter written `<name>`."""
    return replace_parameters(sql, lambda name: f"<{name}>")


def test_reads_a_parameter_outside_quoted_text_the_longest_name_past_ascii_too():
    assert marked("select $id, $idList, $größe, $_x") == "select <id>, <idList>, <größe>, <_x>"


def test_keeps_a_parameter_as_it_is_where_it_is_not_replaced():
    assert replace_parameters("select $a, $b", lambda name: "?" if name == "a" else None) == "select ?, $b"


def test_leaves_a_string_literal_as_it_is_a_doubled_quote_and_all():
    assert marked("select '$t', 'it''s $t', $t") == "select '$t', 'it''s $t', <t>"


def test_leaves_a_quoted_identifier_as_it_is():
    assert marked('select "$t", "a""$t", $t') == 'select "$t", "a""$t", <t>'


def test_leaves_a_national_or_alternatively_quoted_literal_as_it_is():
    assert marked("select n'$a', q'[$b']', NQ'{$c}', q'!$d!', $e") == "select n'$a', q'[$b']', NQ'{$c}', q'!$d!', <e>"


def test_reads_no_literal_where_n_or_q_ends_a_name():
    assert marked("select in'$a', $b") == "select in'$a', <b>"


def test_leaves_postgres_dollar_quoted_text_as_it_is():
    assert marked("select $$ $a $$, $tag$ $b $tag$, $c") == "select $$ $a $$, $tag$ $b $tag$, <c>"


def test_reads_a_dollar_quote_only_where_its_tag_closes_it():
    assert marked("select $a$b") == "select <a>$b"


def test_reads_two_dollars_before_a_name_as_a_plsql_inquiry_directive_not_a_quote_nor_a_parameter():
    assert marked("select $$PLSQL_UNIT, $a from dual") == "select $$PLSQL_UNIT, <a> from dual"


def test_leaves_a_comment_as_it_is():
    assert marked("select /* $a */ $b -- $c\n, $d") == "select /* $a */ <b> -- $c\n, <d>"


def test_reads_no_parameter_inside_an_identifier_nor_a_positional_one():
    assert marked("select v$session, a#$b, $1, $c") == "select v$session, a#$b, $1, <c>"


def test_takes_quoted_text_that_is_never_closed_to_the_end():
    assert marked("select $a, '$b") == "select <a>, '$b"
    assert marked("select $a, /* $b") == "select <a>, /* $b"
    assert marked("select $a, q'[$b") == "select <a>, q'[$b"


def test_leaves_postgres_escaped_literal_as_it_is_a_quote_after_a_backslash_and_all():
    assert marked("select E'it\\'s $a', e'\\\\', $b") == "select E'it\\'s $a', e'\\\\', <b>"


def test_reads_a_lone_dollar_two_dollars_or_q_quote_at_the_end_as_it_is():
    assert marked("select $a, $") == "select <a>, $"
    assert marked("select $a, $$") == "select <a>, $$"
    assert marked("select $a, q'") == "select <a>, q'"


def test_names_the_parameters_outside_quoted_text():
    assert parameter_names("""select $a, '$b', "$c", $a, $d""") == {"a", "d"}


def test_reads_a_name_past_the_bmp_and_no_parameter_after_a_letter_past_it():
    assert marked("select $𝑥, 𝑥$a, $b") == "select <𝑥>, 𝑥$a, <b>"


def test_reads_a_parameter_beside_a_postgres_cast_and_after_one():
    assert marked("select $a::text, x::$b, $c::jsonb") == "select <a>::text, x::<b>, <c>::jsonb"


def test_reads_no_dollar_quote_whose_tag_begins_with_a_digit_and_leaves_a_positional_one_in_a_quote_as_it_is():
    assert marked("select $1$ $a $1$, $t$ $1 $b $t$, $c") == "select $1$ <a> $1$, $t$ $1 $b $t$, <c>"
