"""
The statement the Oracle plan tests explain is written into their PL/SQL block as a string of any length: PL/SQL takes
a literal of 32,767 bytes at most, and a statement compiled knowing a graph's labelled indexes (#508), with a batch's
rows written in, is longer. drivine4j's `OracleSql.text`.
"""

from tests.integration.oracle.oracle_seed import MAX_LITERAL, plsql_text


def test_a_short_text_is_one_literal():
    assert plsql_text("SELECT 'a' FROM DUAL") == "'SELECT ''a'' FROM DUAL'"


def test_a_long_text_is_its_parts_joined_as_a_clob_each_a_literal_oracle_takes():
    text = "x" * MAX_LITERAL + "'" * MAX_LITERAL + "y"
    parts = plsql_text(text).split("\n|| ")
    assert parts == [
        "TO_CLOB('" + "x" * MAX_LITERAL + "')",
        "TO_CLOB('" + "''" * MAX_LITERAL + "')",
        "TO_CLOB('y')",
    ]


def test_each_part_is_a_literal_of_32767_bytes_at_most_whatever_its_characters():
    for character in ("'", "\u20ac", "\U0001f600"):
        parts = plsql_text(character * (3 * MAX_LITERAL)).split("\n|| ")
        assert len(parts) == 3
        assert all(len(it.removeprefix("TO_CLOB(").removesuffix(")").encode()) <= 32767 for it in parts)
