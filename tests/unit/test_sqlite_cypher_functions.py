"""
The functions compiled Cypher calls on SQLite, each asked directly in SQL: what each gives, and what it gives of null,
as drivine4j's `SqliteCypherFunctionsTest` and Drivine for TypeScript's `SqliteCypherFunctions.spec.ts` ask them. Each
value expected is the one drivine4j's functions give, on its SQLite, asked the same SQL.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

from drivine.backend.sqlite import sqlite_graph
from drivine.backend.sqlite.sqlite_functions import Failures, register
from drivine.backend.sqlite.sqlite_patterns import python_pattern
from drivine.drivine_error import DrivineError


class Database:
    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.failures = Failures()
        register(self.connection, self.failures)

    def row(self, sql: str) -> tuple[Any, ...]:
        return self.connection.execute(sql).fetchone()

    def one(self, sql: str) -> Any:
        return self.row(f"SELECT {sql}")[0]

    def fails(self, sql: str) -> BaseException:
        """The failure a function raised in the statement, as the connection raises it in SQLite's place."""
        with pytest.raises(sqlite3.Error, match="user-defined function raised exception"):
            self.row(sql)
        kept = self.failures.take()
        assert kept is not None
        return kept


@pytest.fixture
def db() -> Iterator[Database]:
    database = Database()
    yield database
    database.connection.close()


def test_each_function_is_named_as_drivine4j_names_it():
    names = [
        sqlite_graph.EQUAL, sqlite_graph.CONTAINS, sqlite_graph.ADD, sqlite_graph.CANONICAL, sqlite_graph.CYPHER,
        sqlite_graph.UPPER, sqlite_graph.SPLIT, sqlite_graph.REVERSE, sqlite_graph.UUID, sqlite_graph.REGEX_GROUP,
        sqlite_graph.REGEX_REPLACE, sqlite_graph.TRUNC, sqlite_graph.TO_CHAR, sqlite_graph.TEMPORAL,
        sqlite_graph.FLOAT_TEXT, sqlite_graph.SORT_KEY, sqlite_graph.LIST_ORDER, sqlite_graph.MATH, sqlite_graph.ROUND,
        sqlite_graph.FAIL,
    ]  # fmt: skip
    assert names == [
        "drivine_equal", "drivine_contains", "drivine_add", "drivine_canonical", "drivine_cypher", "drivine_upper",
        "drivine_split", "drivine_reverse", "drivine_uuid", "drivine_regex_group", "drivine_regexp_replace",
        "drivine_trunc", "drivine_to_char", "drivine_temporal", "drivine_float_text", "drivine_sort_key",
        "drivine_list_order", "drivine_math", "drivine_round", "drivine_fail",
    ]  # fmt: skip


def test_a_real_number_given_as_text_is_read_as_drivine4js_sqlite_writes_one(db: Database):
    # 15 digits where they read back as the number, else 17: SQLite 3.49, which Python links, would write 15 always.
    assert db.row(
        "SELECT drivine_upper(2.0), drivine_upper(1e20), drivine_upper(1e-7), drivine_upper(855.4482850851075), "
        "drivine_upper(12), drivine_upper(0.1 + 0.2), drivine_to_char(1e20, 'FM0'), drivine_split(1.5, '.')"
    ) == ("2.0", "1.0E+20", "1.0E-07", "855.4482850851075", "12", "0.30000000000000004", "100000000000000000000",
          '["1","5"]')  # fmt: skip


class TestEqualAndContains:
    def test_compare_as_cypher_does_and_null_with_anything_is_null(self, db: Database):
        assert db.row(
            "SELECT drivine_equal('1', '1.0'), drivine_equal('{\"a\":1,\"b\":[1,2]}', '{\"b\":[1,2.0],\"a\":1}'), "
            "drivine_equal('[1,2]', '[2,1]'), drivine_equal('\"x\"', '\"x\"'), drivine_equal('true', '1'), "
            "drivine_equal('\"7\"', '7')"
        ) == (1, 1, 0, 1, 0, 0)
        assert db.row("SELECT drivine_equal('null', '1'), drivine_equal(NULL, '1'), drivine_equal('  ', '1')") == (
            None,
            None,
            None,
        )

    def test_is_null_where_a_null_a_list_or_map_holds_decides_and_false_where_a_pair_differs(self, db: Database):
        assert db.row(
            "SELECT drivine_equal('[1,null]', '[1,null]'), drivine_equal('[2,null]', '[1,null]'), "
            "drivine_equal('[1]', '[1,null]'), drivine_equal('{\"a\":null}', '{\"b\":null}'), "
            "drivine_equal('9007199254740993', '9007199254740992.0'), drivine_equal('{\"1\":1,\"b\":2}', "
            '\'{"b":2,"1":1}\'), '
            "drivine_equal('{\"a\":{\"b\":null}}', '{\"a\":{\"b\":1}}'), drivine_equal('[true]', '[1]')"
        ) == (None, 0, 0, 0, 0, 1, None, 0)

    def test_compare_numbers_by_their_exact_values(self, db: Database):
        assert db.row(
            "SELECT drivine_equal('123456789012345678901234567890', '123456789012345678901234567890.0'), "
            "drivine_equal('1e2', '100'), drivine_equal('-0', '0.0'), drivine_equal(0.1 + 0.2, '0.3'), "
            "drivine_equal(1, '1.0'), drivine_equal('0.1', 0.1)"
        ) == (0, 1, 1, 0, 1, 1)

    def test_read_json_as_jackson_does_its_first_value_and_refuse_a_root_number_run_on(self, db: Database):
        assert db.row(
            "SELECT drivine_equal('[1] x', '[1]'), drivine_equal('1 x', '1'), drivine_equal('\"a\"x', '\"a\"')"
        ) == (
            1,
            1,
            1,
        )
        for run_on in ("1x", "01", "truex"):
            assert isinstance(db.fails(f"SELECT drivine_equal('{run_on}', '1')"), ValueError)
        # Jackson's BigDecimal has no value of an infinity.
        assert "Infinite" in str(db.fails("SELECT drivine_equal('1e400', '1')"))

    def test_contains_holds_a_maps_every_entry_with_an_equal_value(self, db: Database):
        assert db.row(
            'SELECT drivine_contains(\'{"a":1,"b":2}\', \'{"a":1.0}\'), drivine_contains(\'{"a":1}\', '
            '\'{"a":1,"c":3}\'), '
            "drivine_contains('[1]', '{}'), drivine_contains(NULL, '{}'), drivine_contains('{\"a\":null}', "
            "'{\"a\":null}'), "
            "drivine_contains('{\"a\":[1,null]}', '{\"a\":[1,null]}')"
        ) == (1, 0, 0, None, 1, 1)


class TestAdd:
    def test_sums_joins_lists_takes_a_null_into_a_list_and_writes_texts_and_numbers_one_after_the_other(
        self, db: Database
    ):
        assert db.row(
            "SELECT drivine_add('1', '2'), drivine_add('1', '2.5'), drivine_add('[1]', '[2,3]'), drivine_add('[1]', "
            "NULL), "
            "drivine_add('0', '[1]'), drivine_add('\"a\"', '1'), drivine_add('1.5', '\"b\"'), drivine_add('true', "
            "'\"b\"'), "
            "drivine_add('1', NULL), drivine_add(NULL, '[1]'), drivine_add('{\"a\":1}', '{\"b\":1}')"
        ) == ("3", "3.5", "[1,2,3]", "[1,null]", "[0,1]", '"a1"', '"1.5b"', None, None, "[null,1]", None)

    def test_stays_whole_past_a_long_and_writes_a_double_as_java_does(self, db: Database):
        assert db.row(
            "SELECT drivine_add('9223372036854775807', '9223372036854775807'), drivine_add('1e2', '0'), "
            "drivine_add('\"x\"', '1e7'), drivine_add('[{\"b\":1,\"1\":2}]', '1.50'), drivine_add('\"\\u0001\"', "
            "'\"\"'), "
            "drivine_add('0.1', '0.2'), drivine_add('-9223372036854775808', '-1'), drivine_add('9007199254740993', "
            "'0.0'), "
            "drivine_add('\"x\"', '0.001'), drivine_add('\"x\"', '1e-5')"
        ) == ("18446744073709551614", "100.0", '"x1.0E7"', '[{"b":1,"1":2},1.5]', '"\\u0001"', "0.30000000000000004",
              "-9223372036854775809", "9.007199254740992E15", '"x0.001"', '"x1.0E-5"')  # fmt: skip

    def test_adds_a_whole_number_already_past_a_long_exactly_not_by_its_low_64_bits(self, db: Database):
        # liberation-data/drivine4j#547: drivine4j gives -4362896299872285997 here.
        assert db.one("drivine_add('123456789012345678901234567890', '1')") == "123456789012345678901234567891"
        assert db.one("drivine_add('18446744073709551617', '1')") == "18446744073709551618"

    def test_writes_a_text_as_jackson_escapes_one_and_an_infinity_as_a_text(self, db: Database):
        assert db.one('drivine_add(\'"\\u001f\\b\\t\\n\\f\\r\\"\\\\/\\u007f\\u2028😀"\', \'""\')') == (
            '"\\u001F\\b\\t\\n\\f\\r\\"\\\\/\x7f\u2028😀"'
        )
        assert db.one("drivine_add('1e308', '1e308')") == '"Infinity"'


def test_canonical_puts_a_maps_keys_in_order_at_any_depth_by_their_utf16_units(db: Database):
    assert db.row(
        "SELECT drivine_canonical('{\"b\":1,\"a\":[{\"y\":2,\"x\":1}]}'), drivine_canonical(NULL), "
        "drivine_canonical('null'), drivine_canonical('{\"b\":1.50,\"2\":0,\"10\":0}'), "
        "drivine_canonical('{\"\\uffff\":1,\"😀\":2,\"a\":3}'), drivine_canonical('{\"a\":1,\"a\":2,\"b\":0}'), "
        "drivine_canonical('1e5'), drivine_canonical(7)"
    ) == ('{"a":[{"x":1,"y":2}],"b":1}', None, "null", '{"10":0,"2":0,"b":1.5}', '{"a":3,"😀":2,"\uffff":1}',
          '{"a":2,"b":0}', "100000.0", "7")  # fmt: skip


class TestTextFunctions:
    def test_raise_split_reverse_and_make_a_uuid(self, db: Database):
        assert db.row(
            "SELECT drivine_upper('école'), drivine_split('a,,b,', ','), drivine_split('a😀', ''), drivine_split('', "
            "','), "
            "drivine_reverse('ab😀'), drivine_split(NULL, ','), drivine_upper('straße'), drivine_upper(NULL), "
            "drivine_split('aaa', 'aa'), drivine_reverse('e\u0301x')"
        ) == ("ÉCOLE", '["a","","b",""]', '["a","😀"]', '[""]', "😀ba", None, "STRASSE", None, '["","a"]',
              "x\u0301e")  # fmt: skip
        assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}", db.one("drivine_uuid()"))
        assert db.one("drivine_uuid() <> drivine_uuid()") == 1

    def test_raise_and_lower_as_javas_root_locale_does(self, db: Database):
        assert db.row("SELECT drivine_upper('ﬀ ŉ ᾳ ß ǆ ı i'), drivine_lower('ΟΔΟΣ İ ΣΑ Σ'), drivine_lower(NULL)") == (
            "FF ʼN ΑΙ SS Ǆ I I",
            "οδος i̇ σα σ",
            None,
        )


class TestRegularExpressions:
    def test_read_a_group_and_replace_matches_naming_groups_with_a_backslash(self, db: Database):
        assert db.row(
            "SELECT drivine_regex_group('10:00+02:00', '([+-][0-9]{2}:?[0-9]{2})$', 1), "
            "drivine_regex_group('10:00', 'Z$', 1), "
            "drivine_regexp_replace('+0200', '^([+-][0-9]{2})([0-9]{2})$', '\\1:\\2'), "
            "drivine_regexp_replace('ab', 'a', 'a$1')"
        ) == ("+02:00", None, "+02:00", "a$1b")

    def test_give_null_of_a_group_past_the_expression_or_one_that_takes_no_part(self, db: Database):
        assert db.row(
            "SELECT drivine_regex_group('ab', '(a)(x)?', 2), drivine_regex_group('ab', '(a)', 2), "
            "drivine_regex_group('ab', '(a)b', 0), drivine_regex_group(NULL, 'a', 1), drivine_regex_group('ab', 'a', "
            "NULL), "
            "drivine_regex_group('ab', '(a)', 4294967297), drivine_regex_group('ab', '(a)', '1')"
        ) == (None, None, "ab", None, "a", "a", "a")
        assert str(db.fails("SELECT drivine_regex_group('ab', 'a', -1)")) == "No group -1"

    def test_replace_with_the_whole_match_a_literal_backslash_and_nothing_for_a_group_that_takes_no_part(
        self, db: Database
    ):
        assert db.row(
            "SELECT drivine_regexp_replace('abc', 'b', '[\\0]'), drivine_regexp_replace('abc', 'b', '\\x'), "
            "drivine_regexp_replace('abc', '(b)(z)?', '<\\2>'), drivine_regexp_replace('abc', '(b)', '\\10'), "
            "drivine_regexp_replace(NULL, 'b', 'x'), drivine_regexp_replace('abc', 'x*', '-')"
        ) == ("a[b]c", "a\\xc", "a<>c", "ab0c", None, "-a-b-c-")
        assert str(db.fails("SELECT drivine_regexp_replace('abc', 'b', '\\2')")) == "No group 2"
        assert str(db.fails("SELECT drivine_regexp_replace('abc', 'b', '\\\\1')")) == "No group 1"

    def test_read_a_flag_group_anywhere_from_where_it_is_to_the_end_of_its_group_as_java_does(self, db: Database):
        assert db.row(
            "SELECT 'ABC' REGEXP '^(?:(?i)abc)\\z', 'ABC' REGEXP '^(?:abc)\\z', 'xABC' REGEXP '^(?:x(?i)abc)\\z', "
            "'XABC' REGEXP '^(?:x(?i)abc)\\z', 'a[(?i)]' REGEXP '\\[\\(\\?i\\)\\]', "
            "'xAbC' REGEXP '^(?:x(?i)a(?-i)b(?i)c)\\z', 'xABC' REGEXP '^(?:x(?i)a(?-i)b(?i)c)\\z', "
            "'Ab' REGEXP '^(?:(?i)a(?-i:b))\\z', 'AB' REGEXP '^(?:(?i)a(?-i:b))\\z', 'AY' REGEXP '^(?:(a(?i))y)\\z'"
        ) == (1, 0, 1, 0, 1, 1, 0, 1, 0, 0)
        assert python_pattern("^(?:x(?i)abc)\\z") == "\\A(?:x(?i:abc))\\Z"

    def test_keep_a_flag_group_in_force_past_a_bar_to_the_end_of_its_group(self, db: Database):
        assert db.row(
            "SELECT 'C' REGEXP 'a(?i)b|c', 'A' REGEXP '^(?:a|(?i)b)\\z', 'B' REGEXP '^(?:a|(?i)b)\\z', "
            "'C' REGEXP '^(?:a|(?i)b|c)\\z', 'Y' REGEXP '^(?:(?i)(x)|y)\\z'"
        ) == (1, 0, 1, 1, 1)
        assert python_pattern("a(?i)b|c") == "a(?i:b)|(?i:c)"

    def test_read_flags_python_has_none_of_as_java_does(self, db: Database):
        assert db.row(
            "SELECT 'a b' REGEXP '^(?:(?x) a \\  b )\\z', 'a#b' REGEXP '^(?:(?x)a\\#b)\\z', "
            "'aB' REGEXP '^(?:(?ix)a b)\\z', "
            "'A' REGEXP '^(?:(?u)(?i)a)\\z', 'A' REGEXP '^(?:(?U)a)\\z', 'a' || char(13) || 'b' REGEXP '(?d)a.b', "
            "'ab' REGEXP '(?x)a#c' || char(10) || 'b'"
        ) == (1, 1, 1, 1, 0, 1, 1)

    def test_read_z_as_the_text_end_and_an_escaped_backslash_before_a_z_as_itself(self, db: Database):
        assert db.row(
            "SELECT 'a\\z' REGEXP '^(?:a\\\\z)\\z', 'a' REGEXP '^(?:a)\\z', 'ab' REGEXP '^(?:a)\\z', "
            "'a' || char(10) REGEXP '^(?:a)\\z', 'a' || char(10) REGEXP '^(?:a)$'"
        ) == (1, 1, 0, 0, 1)

    def test_know_each_of_javas_line_terminators(self, db: Database):
        assert db.row(
            "SELECT 'a' || char(13) || 'b' REGEXP 'a.b', 'a' || char(8232) || 'b' REGEXP 'a.b', "
            "'a' || char(10) || 'b' REGEXP '(?s)a.b', 'x' || char(13) || 'b' REGEXP '(?m)^b', "
            "'b' || char(13) || 'x' REGEXP '(?m)b$', 'x' || char(13,10) REGEXP 'x$', 'x' || char(133) REGEXP 'x\\Z', "
            "'x' || char(10) || char(10) REGEXP 'x$', 'x' || char(13,10) || 'y' REGEXP '(?m)^y'"
        ) == (0, 0, 1, 1, 1, 1, 1, 0, 1)

    def test_read_word_space_and_digit_classes_as_javas_unicode_classes(self, db: Database):
        # liberation-data/drivine#560's cases, and where Python's own classes differ from Java's.
        assert db.row(
            "SELECT 'café' REGEXP '^\\w+$', drivine_regexp_count('naïve café', '\\b\\w+\\b'), 'a_b' REGEXP 'a\\_b', "
            "'e' || char(769) REGEXP '^\\w+$', '²' REGEXP '^\\w$', char(28) REGEXP '\\s', char(160) REGEXP '\\s', "
            "'١٢' REGEXP '^\\d+$', 'ⅷ' REGEXP '\\w', '½' REGEXP '\\w', char(8205) REGEXP '\\w'"
        ) == (1, 2, 1, 1, 0, 0, 1, 1, 1, 0, 1)
        assert db.row(
            "SELECT 'café' REGEXP '\\bcaf\\b', 'café' REGEXP '\\bcafé\\b', 'ab' REGEXP 'a\\Bb', "
            "drivine_regexp_count('one two  three', '\\b'), ' ' REGEXP '\\h', char(10) REGEXP '\\h', "
            "'a' || char(13,10) REGEXP '^a\\R\\z'"
        ) == (0, 1, 1, 6, 1, 0, 1)

    def test_read_unicode_properties_and_posix_names_as_java_does(self, db: Database):
        assert db.row(
            "SELECT 'Ⓐ' REGEXP '\\p{Alpha}', 'ⓐ' REGEXP '\\p{Lower}', 'ा' REGEXP '\\p{Alpha}', "
            "'́' REGEXP '\\p{Alpha}', "
            "'ab12' REGEXP '^\\p{L}+\\p{N}+$', 'ab' REGEXP '^\\p{IsL}+$', 'A' REGEXP '\\p{gc=Lu}', "
            "'A' REGEXP '[\\P{Lu}]', "
            "'$' REGEXP '\\p{Punct}', '!' REGEXP '\\p{Punct}', 'é' REGEXP '\\p{L1}', 'ǅ' REGEXP '\\p{IsTitlecase}', "
            "char(10) REGEXP '\\p{Blank}', '١' REGEXP '[[:digit:]]', 'a' REGEXP '(?i)\\p{Lu}'"
        ) == (1, 1, 1, 0, 1, 1, 1, 0, 0, 1, 1, 1, 0, 1, 1)

    def test_refuse_a_script_or_block_python_has_no_table_of_rather_than_read_it_otherwise(self, db: Database):
        for sql in ("SELECT 'x' REGEXP '\\p{IsLatin}'", "SELECT 'x' REGEXP '\\p{InGreek}'", "SELECT 'x' REGEXP '\\G'"):
            refused = db.fails(sql)
            assert isinstance(refused, DrivineError) and "is not read on SQLite here" in str(refused)

    def test_read_possessive_and_atomic_groups_names_back_references_and_quoted_text(self, db: Database):
        assert db.row(
            "SELECT 'aaa' REGEXP '^a*+a', 'aab' REGEXP '^(?:(?>a+)b)\\z', 'a1a' REGEXP '^(?:(?<x>a)1\\k<x>)\\z', "
            "'abcdefghijkk' REGEXP '^(?:(a)(b)(c)(d)(e)(f)(g)(h)(i)(j)(k)\\11)\\z', "
            "'abcdefghija1' REGEXP '^(?:(a)(b)(c)(d)(e)(f)(g)(h)(i)(j)\\11)\\z', 'aa0' REGEXP '^(?:(a)\\10)\\z', "
            "'a.b' REGEXP '^(?:\\Qa.b\\E)\\z', 'axb' REGEXP '^a\\Q.\\Eb$', '😀' REGEXP '^\\x{1F600}$', "
            "'😀' REGEXP '^\\uD83D\\uDE00$', 'a' || char(27) REGEXP 'a\\e', char(1) REGEXP '\\cA', '@' REGEXP '\\0100'"
        ) == (0, 1, 1, 1, 1, 1, 1, 0, 1, 1, 1, 1, 1)

    def test_work_out_a_class_within_a_class_and_an_intersection(self, db: Database):
        assert db.row(
            "SELECT 'b' REGEXP '[a-c&&[^b]]', 'a' REGEXP '[a-c&&[^b]]', 'x' REGEXP '[a-c[x-z]]', "
            "'é' REGEXP '[\\p{L}&&[^a-z]]', 'c' REGEXP '[a-z&&b-c&&b]', '5' REGEXP '[^0-4[6]]', "
            "'6' REGEXP '[^0-4[6]]', "
            "'a' REGEXP '[a&&]', 'a' REGEXP '[&&a]', ']' REGEXP '[]]', 'A' REGEXP '(?i)[^a]', '-' REGEXP '[\\w-]'"
        ) == (0, 1, 1, 1, 0, 1, 0, 1, 1, 1, 0, 1)

    def test_count_matches_as_an_integer(self, db: Database):
        assert db.row("SELECT drivine_regexp_count('aXa', 'a'), drivine_regexp_count(NULL, 'a')") == (2, 0)

    def test_leave_a_group_not_closed_for_python_to_refuse_as_itself(self, db: Database):
        assert isinstance(db.fails("SELECT 'a' REGEXP '(a(?i)b'"), re.error)


class TestTrunc:
    def test_cuts_to_a_whole_number(self, db: Database):
        assert db.row(
            "SELECT drivine_trunc(2.9), drivine_trunc(-2.9), drivine_trunc(7), drivine_trunc(NULL), "
            "drivine_trunc(1e300), "
            "drivine_trunc('2.5'), drivine_trunc('x'), drivine_trunc(-9.3e18)"
        ) == (2, -2, 7, None, 1e300, 2, 0, -9.3e18)

    def test_takes_a_real_number_a_unit_of_its_last_place_short_of_a_whole_number_as_that_number(self, db: Database):
        assert db.row(
            "SELECT drivine_trunc(33.001 * 1000000), drivine_trunc(-33.001 * 1000000), "
            "drivine_trunc(3599.999999 / 3600), "
            "drivine_trunc(9223372036854775807)"
        ) == (33001000, -33001000, 0, 9223372036854775807)


class TestToChar:
    def test_writes_a_timestamp_or_a_number_in_a_pattern(self, db: Database):
        assert db.row(
            "SELECT drivine_to_char('2024-03-15T10:30:05.000000', 'YYYY-MM-DD\"T\"HH24:MI:SS'), "
            "drivine_to_char('2024-12-30T00:00:00.000000', 'IYYY'), drivine_to_char('2024-03-15T00:00:00.000000', "
            "'IW'), "
            "drivine_to_char('2024-03-15T00:00:00.000000', 'DDD'), drivine_to_char(42, 'FM0000'), "
            "drivine_to_char(2.5, 'FM9990.999'), drivine_to_char(3, 'FM9990.999'), drivine_to_char(-1.25, "
            "'FM9990.999'), "
            "drivine_to_char(NULL, 'FM00')"
        ) == ("2024-03-15T10:30:05", "2025", "11", "075", "0042", "2.5", "3.", "-1.25", None)

    def test_writes_a_year_before_1_as_iso_8601_numbers_it(self, db: Database):
        assert db.row(
            "SELECT drivine_to_char('-0044-03-15T10:30:05.000000', 'YYYY-MM-DD\"T\"HH24:MI:SS'), "
            "drivine_to_char('0000-01-01T00:00:00.000000', 'YYYY-MM-DD'), "
            "drivine_to_char('0000-01-01T00:00:00.000000', 'IYYY'), "
            "drivine_to_char('-1234-06-01T00:00:00.000000', 'YYYY'), drivine_to_char('+12345-06-01T00:00:00.000000', "
            "'YYYY')"
        ) == ("-0044-03-15T10:30:05", "0000-01-01", "-0001", "-1234", "12345")

    def test_rounds_a_number_half_up_in_decimal_and_writes_zero_without_a_sign(self, db: Database):
        assert db.row(
            "SELECT drivine_to_char(2.345, 'FM0.00'), drivine_to_char(-2.5, 'FM0'), drivine_to_char(-0.0001, "
            "'FM0.99'), "
            "drivine_to_char(0.5, 'FM0'), drivine_to_char(1.0 / 3, 'FM0.9999999999999999'), "
            "drivine_to_char('2024-01-01 10:00:00', 'HH24\"h\"'), drivine_to_char(30, 'FM90.'), "
            "drivine_to_char('2.5', 'FM0'), "
            "drivine_to_char(-0.4, 'FM0'), drivine_to_char(1e-30, 'FM0.99')"
        ) == ("2.35", "-3", "0.", "1", "0.3333333333333333", "10h", "30.", "3", "0", "0.")

    def test_numbers_iso_weeks_as_java_time_does_at_the_ends_of_a_year(self, db: Database):
        assert db.row(
            "SELECT drivine_to_char('2021-01-03T00:00:00.000000', 'IYYY-IW'), "
            "drivine_to_char('2020-12-31T00:00:00.000000', "
            "'IYYY-IW'), drivine_to_char('2026-01-01T00:00:00.000000', 'IYYY-IW'), "
            "drivine_to_char('2024-12-31T00:00:00.000000', 'DDD')"
        ) == ("2020-53", "2020-53", "2026-01", "366")


def test_float_text_writes_a_real_as_java_writes_a_double(db: Database):
    assert db.row(
        "SELECT drivine_float_text(2.0), drivine_float_text(1e7), drivine_float_text(1.2e-4), "
        "drivine_float_text(1.0 / 3), "
        "drivine_float_text(-2.5), drivine_float_text(NULL), drivine_float_text(3), drivine_float_text(5e-324), "
        "drivine_float_text(1e23), drivine_float_text(0.001), drivine_float_text(9999999.0), drivine_float_text(-0.0), "
        "drivine_float_text('abc'), drivine_float_text(2e22)"
    ) == ("2.0", "1.0E7", "1.2E-4", "0.3333333333333333", "-2.5", None, "3.0", "4.9E-324", "1.0E23", "0.001",
          "9999999.0", "-0.0", "0.0", "2.0E22")  # fmt: skip


class TestSortKey:
    def test_orders_lists_as_cypher_does_a_text_before_one_it_begins(self, db: Database):
        lists = (
            '[[1, null], ["ab"], [true], [], [1e300], ["a\\u0002"], [[1]], [-1], [null], ["a"], [{"a": 1}], [1, 2], '
            '[false], ["a\\u0001"], [-2.5], [1], [0]]'
        )
        assert (
            db.one(f"(SELECT json_group_array(t.key ORDER BY drivine_sort_key(t.value)) FROM json_each('{lists}') t)")
            == "[3,10,6,9,13,5,1,12,2,14,7,16,15,11,0,4,8]"
        )

    def test_leaves_any_other_value_as_it_is(self, db: Database):
        assert db.row("SELECT drivine_sort_key('\"x\"'), drivine_sort_key(NULL), drivine_sort_key('null')") == (
            '"x"',
            None,
            None,
        )


class TestListOrder:
    def test_orders_two_lists_as_cypher_compares_them(self, db: Database):
        assert db.row(
            "SELECT drivine_list_order('[1, 2]', '[1, 3]'), drivine_list_order('[1, 2.0]', '[1.0, 2]'), "
            "drivine_list_order('[[1, 2]]', '[[1]]'), drivine_list_order('[\"\\uffff\"]', '[\"\\ud83d\\ude00\"]'), "
            "drivine_list_order('[9007199254740993]', '[9007199254740992]'), drivine_list_order('[false]', '[true]'), "
            "drivine_list_order('[{\"a\": 1}]', '[{\"a\": 1.0}]'), drivine_list_order('[1]', '[]')"
        ) == (-1, 0, 1, -1, 1, -1, 0, 1)

    def test_gives_null_where_either_is_no_list_or_the_pair_that_decides_is_not_ordered(self, db: Database):
        assert db.row(
            "SELECT drivine_list_order('[null]', '[1]'), drivine_list_order('[1]', '[\"1\"]'), "
            "drivine_list_order('[{\"a\": 1}]', '[{\"a\": 2}]'), drivine_list_order('1', '[1]'), "
            "drivine_list_order('[1]', '{}'), drivine_list_order(NULL, '[1]'), drivine_list_order('[true]', '[1]')"
        ) == (None, None, None, None, None, None, None)


class TestMath:
    def test_works_out_opencyphers_functions_of_numbers_as_javas_math_does(self, db: Database):
        found = db.row(
            "SELECT drivine_math('exp', 0), drivine_math('log10', 1000), drivine_math('sqrt', 16), "
            "drivine_math('atan2', 1, 1), drivine_math('degrees', pi()), drivine_math('radians', 180), "
            "drivine_math('haversin', 0), drivine_math('sin', NULL), typeof(drivine_math('sqrt', 16)), "
            "drivine_math('degrees', 1), drivine_math('radians', 1), drivine_math('sqrt', '2')"
        )
        assert found == (1.0, 3.0, 4.0, math.pi / 4, 180.0, math.pi, 0.0, None, "real", 57.29577951308232,
                         0.017453292519943295, 1.4142135623730951)  # fmt: skip
        assert db.one("drivine_math('cot', 1)") == pytest.approx(1 / math.tan(1))

    def test_fails_where_java_gives_nan_or_an_infinity_which_json_holds_no_value_for(self, db: Database):
        for sql, message in [
            ("drivine_math('log', 0)", "log() of 0.0 is -Infinity, which JSON holds no value for"),
            ("drivine_math('sqrt', -1)", "sqrt() of -1.0 is NaN"),
            ("drivine_math('exp', 1000)", "exp() of 1000.0 is Infinity"),
            ("drivine_math('cot', 0)", "cot() of 0.0 is Infinity"),
            ("drivine_math('asin', 2)", "asin() of 2.0 is NaN"),
            ("drivine_math('nope', 1)", "drivine_math has no function nope."),
        ]:
            assert message in str(db.fails(f"SELECT {sql}"))


class TestRound:
    def test_rounds_in_decimal_by_each_mode(self, db: Database):
        assert db.row(
            "SELECT drivine_round(2.345, 2, 'HALF_UP'), drivine_round(2.345, 2, 'HALF_DOWN'), "
            "drivine_round(2.345, 2, 'HALF_EVEN'), drivine_round(2.355, 2, 'HALF_EVEN'), "
            "drivine_round(-2.341, 2, 'CEILING'), drivine_round(-2.341, 2, 'FLOOR'), drivine_round(2.341, 2, 'UP'), "
            "drivine_round(-2.349, 2, 'DOWN'), drivine_round(-2.5, 0, 'HALF_UP')"
        ) == (2.35, 2.34, 2.34, 2.36, -2.34, -2.35, 2.35, -2.34, -3.0)

    def test_leaves_a_number_of_no_more_places_as_it_is_and_gives_a_real_number_of_a_whole_one(self, db: Database):
        assert db.row(
            "SELECT drivine_round(2.5, 1000000000, NULL), drivine_round(7, 0, 'HALF_UP'), typeof(drivine_round(7, 0, "
            "'UP')), "
            "drivine_round(1234.5, -2, 'HALF_UP'), drivine_round(NULL, 1, 'UP'), drivine_round(1.5, NULL, 'UP'), "
            "drivine_round(1e10, 0, 'UNNECESSARY'), drivine_round(2.5, 4294967296, 'HALF_UP'), "
            "drivine_round('2.25', 1, 'HALF_UP'), drivine_round(123456789012345678, -5, 'HALF_UP')"
        ) == (2.5, 7.0, "real", 1200.0, None, None, 1e10, 2.5, 2.3, 1.234567890123e17)
        assert str(db.fails("SELECT drivine_round(2.25, 1, 'UNNECESSARY')")) == "Rounding necessary"
        assert "RoundingMode.SIDEWAYS" in str(db.fails("SELECT drivine_round(2.25, 1, 'SIDEWAYS')"))


class TestTemporal:
    def test_reads_moves_measures_and_cuts_a_timestamp(self, db: Database):
        assert db.row(
            "SELECT drivine_temporal('local', '2024-03-15', 'DATE'), "
            "drivine_temporal('utc', '2024-03-15T10:30+0200', 'DATE_TIME'), drivine_temporal('utc', '10:00+01:00', "
            "'TIME'), "
            "drivine_temporal('add_months', '2024-01-31T10:00:00.000000', 1), "
            "drivine_temporal('add_time', '2024-03-15T10:00:00.000000', 1, 1.5), "
            "drivine_temporal('months_between', '2024-01-31T00:00:00.000000', '2024-03-31T00:00:00.000000'), "
            "drivine_temporal('seconds_between', '2024-03-15T10:00:00.500000', '2024-03-15T10:00:00.000000'), "
            "drivine_temporal('truncate', '2024-03-15T10:30:00.000000', 'week'), "
            "drivine_temporal('extract', '2024-03-15T10:30:30.250000', 'SECOND')"
        ) == ("2024-03-15T00:00:00.000000", "2024-03-15T08:30:00.000000", "1970-01-01T09:00:00.000000",
              "2024-02-29T10:00:00.000000", "2024-03-16T10:00:01.500000", 2, -0.5, "2024-03-11T00:00:00.000000",
              30.25)  # fmt: skip

    def test_rounds_a_number_of_months_that_is_no_whole_number_half_away_from_zero(self, db: Database):
        assert db.row(
            "SELECT drivine_temporal('add_months', '2024-01-31T10:00:00.000000', 1.5), "
            "drivine_temporal('add_months', '2024-01-31T10:00:00.000000', -1.5), "
            "drivine_temporal('add_months', '2024-01-31T10:00:00.000000', '2.5')"
        ) == ("2024-03-31T10:00:00.000000", "2023-11-30T10:00:00.000000", "2024-04-30T10:00:00.000000")

    def test_reads_a_year_before_1_which_sqlites_own_date_functions_dont(self, db: Database):
        assert db.row(
            "SELECT drivine_temporal('local', '-0044-03-15', 'DATE'), "
            "drivine_temporal('day_of_week', '-0044-03-15T00:00:00.000000'), "
            "drivine_temporal('extract', '-0044-03-15T00:00:00.000000', 'YEAR'), "
            "drivine_temporal('months_between', '-0001-03-15T10:00:00.000000', '0001-01-15T11:00:00.000000')"
        ) == ("-0044-03-15T00:00:00.000000", 4, -44, 22)

    def test_counts_months_as_a_calendar_does_a_time_of_day_not_yet_come_round_not_counting(self, db: Database):
        assert db.row(
            "SELECT drivine_temporal('months_between', '2024-01-31T10:00:00.000000', '2024-02-29T09:00:00.000000'), "
            "drivine_temporal('months_between', '2024-03-15T10:00:00.000000', '2024-01-15T10:00:00.000000'), "
            "drivine_temporal('months_between', '2024-03-15T10:00:00.000000', '2024-01-15T11:00:00.000000'), "
            "drivine_temporal('seconds_between', '2024-03-15T10:00:00.000000', '2024-03-16T10:00:01.250000')"
        ) == (0, -2, -1, 86401.25)

    def test_cuts_to_each_unit_as_neo4j_cuts(self, db: Database):
        def cut(unit: str, at: str = "2017-11-11T12:31:14.645876123") -> str:
            return db.one(f"drivine_temporal('truncate', '{at}', '{unit}')")

        units = ["millennium", "century", "decade", "year", "weekYear", "quarter", "month", "day", "hour", "minute"]
        assert [cut(it) for it in units] == [
            "2000-01-01T00:00:00.000000", "2000-01-01T00:00:00.000000", "2010-01-01T00:00:00.000000",
            "2017-01-01T00:00:00.000000", "2017-01-02T00:00:00.000000", "2017-10-01T00:00:00.000000",
            "2017-11-01T00:00:00.000000", "2017-11-11T00:00:00.000000", "2017-11-11T12:00:00.000000",
            "2017-11-11T12:31:00.000000",
        ]  # fmt: skip
        assert [cut("second"), cut("millisecond"), cut("microsecond"), cut("millennium", "-1234-05-06T00:00")] == [
            "2017-11-11T12:31:14.000000",
            "2017-11-11T12:31:14.645000",
            "2017-11-11T12:31:14.645876",
            "-1000-01-01T00:00:00.000000",
        ]
        assert cut("weekYear", "2021-01-03T00:00") == "2019-12-30T00:00:00.000000"
        assert cut("decade", "-0005-05-06T00:00") == "0000-01-01T00:00:00.000000"
        assert str(db.fails("SELECT drivine_temporal('truncate', '2024-01-01T00:00', 'fortnight')")) == (
            "A timestamp is not truncated to fortnight."
        )

    def test_reads_a_local_text_of_each_kind_its_fraction_cut_to_the_microsecond(self, db: Database):
        assert db.row(
            "SELECT drivine_temporal('local', '2024-03-15T10:30:00.1234567+05:30', 'DATE_TIME'), "
            "drivine_temporal('local', '10:30', 'LOCAL_TIME'), drivine_temporal('utc', '2024-03-15T10:30', "
            "'LOCAL_DATE_TIME'), "
            "drivine_temporal('utc', '00:30Z', 'TIME'), drivine_temporal('utc', '2024-01-01T00:30-0100', 'DATE_TIME'), "
            "drivine_temporal('utc', '00:30+01:00', 'TIME'), drivine_temporal('local', '2024-03-15t10:30', "
            "'LOCAL_DATE_TIME')"
        ) == ("2024-03-15T10:30:00.123456", "1970-01-01T10:30:00.000000", "2024-03-15T10:30:00.000000",
              "1970-01-01T00:30:00.000000", "2024-01-01T01:30:00.000000", "1969-12-31T23:30:00.000000",
              "2024-03-15T10:30:00.000000")  # fmt: skip

    def test_is_null_where_an_argument_is_and_fails_on_a_text_that_names_no_value(self, db: Database):
        assert db.one("drivine_temporal('local', NULL, 'DATE')") is None
        for sql, message in [
            (
                "drivine_temporal('local', '2024-02-30', 'DATE')",
                "'2024-02-30' is no temporal value: Text '2024-02-30' could not be parsed: Invalid date 'FEBRUARY 30'",
            ),
            ("drivine_temporal('local', '2023-02-29', 'DATE')", "is not a leap year"),
            ("drivine_temporal('local', 'P1D', 'DURATION')", "A duration is no moment."),
            ("drivine_temporal('utc', '2024-01-01T00:00+1900', 'DATE_TIME')", "Zone offset not in valid range"),
            ("drivine_temporal('jump', '2024-01-01')", "drivine_temporal has no operation jump."),
            (
                "drivine_temporal('local', '2024-01-01', 'NOPE')",
                "No enum constant org.drivine.cypher.sql.Temporal.NOPE",
            ),
        ]:
            assert message in str(db.fails(f"SELECT {sql}"))


def test_fail_fails_the_statement_with_its_text(db: Database):
    assert str(db.fails("SELECT drivine_fail('round() of a precision below 0')")) == "round() of a precision below 0"
    assert db.one("CASE WHEN 1 = 0 THEN drivine_fail('never') ELSE 'fine' END") == "fine"


class TestCypher:
    @staticmethod
    def steps(*given: dict[str, Any]) -> str:
        return json.dumps({"steps": list(given)}).replace("'", "''")

    SET = {"op": "select", "sql": "SELECT '{\"n\":1}'", "variables": [], "variable": "cy_value"}
    INSERT = {
        "op": "execute",
        "sql": "INSERT INTO cyfunctions_node (labels, props) VALUES ('[]', ?1) RETURNING nid",
        "variables": ["cy_value"],
        "variable": "cy_id",
    }
    RETURNED = {"op": "return", "sql": "SELECT json_quote(?1) AS result", "variables": ["cy_id"]}

    @pytest.fixture
    def graph(self, db: Database) -> Database:
        for it in sqlite_graph.SqliteGraph("cyfunctions").ddl:
            db.connection.execute(it)
        return db

    def count(self, db: Database) -> int:
        return db.one("count(*) FROM cyfunctions_node")

    def test_carries_out_its_steps_and_gives_the_rows_it_returns_as_a_json_list(self, graph: Database):
        assert graph.one(f"drivine_cypher('{self.steps(self.SET, self.INSERT, self.RETURNED)}', '{{}}')") == "[1]"
        assert graph.one("props FROM cyfunctions_node") == '{"n":1}'

    def test_binds_a_variable_that_holds_a_number_as_one_and_not_as_its_text(self, graph: Database):
        real = {"op": "select", "sql": "SELECT 1.5", "variables": [], "variable": "cy_value"}
        big = {"op": "select", "sql": "SELECT 9007199254740993", "variables": [], "variable": "cy_id"}
        typed = {
            "op": "return",
            "sql": "SELECT json_quote(?1) AS result UNION ALL SELECT json_quote(?2) UNION ALL SELECT ?1 "
            "UNION ALL SELECT 1e20 UNION ALL SELECT NULL",
            "variables": ["cy_value", "cy_id"],
        }
        assert graph.one(f"drivine_cypher('{self.steps(real, big, typed)}', '{{}}')") == (
            "[1.5,9007199254740993,1.5,1.0E20,null]"
        )

    def test_binds_the_parameters_loops_over_rows_read_before_the_loop_writes_and_runs_a_body_where_it_holds(
        self, graph: Database
    ):
        program = self.steps(
            {
                "op": "loop",
                "sql": "SELECT value FROM json_each(?1 ->> '$.names')",
                "variables": ["cy_params"],
                "record": "cy_record.item",
                "body": [
                    {
                        "op": "execute",
                        "sql": "INSERT INTO cyfunctions_node (labels, props) VALUES ('[]', json_object('name', ?1))",
                        "variables": ["cy_record.item"],
                        "variable": None,
                    },
                    {
                        "op": "if",
                        "sql": "SELECT CASE WHEN ?1 = 'b' THEN 1 ELSE 0 END",
                        "variables": ["cy_record.item"],
                        "body": [{"op": "return", "sql": "SELECT json_quote(?1)", "variables": ["cy_record.item"]}],
                    },
                ],
            },
            {
                "op": "loop",
                "sql": "SELECT nid FROM cyfunctions_node",
                "variables": [],
                "record": "cy_matched.item",
                "body": [
                    {
                        "op": "execute",
                        "sql": "INSERT INTO cyfunctions_node (labels, props) VALUES ('[]', '{}')",
                        "variables": [],
                        "variable": None,
                    }
                ],
            },
            {"op": "return", "sql": "SELECT count(*) FROM cyfunctions_node", "variables": []},
        )
        assert graph.one(f'drivine_cypher(\'{program}\', \'{{"names":["a","b","c"]}}\')') == '["b",6]'

    def test_reads_a_condition_as_kotlins_to_int_and_a_variable_not_set_as_null(self, graph: Database):
        program = self.steps(
            {"op": "if", "sql": "SELECT 1.7", "variables": [], "body": [{"op": "return", "sql": "SELECT 'yes'"}]},
            {"op": "if", "sql": "SELECT 4294967297", "variables": [], "body": [{"op": "return", "sql": "SELECT 'w'"}]},
            {"op": "if", "sql": "SELECT '1'", "variables": [], "body": [{"op": "return", "sql": "SELECT 'text'"}]},
            {"op": "return", "sql": "SELECT json_quote(?1)", "variables": ["cy_nothing"]},
        )
        assert graph.one(f"drivine_cypher('{program}', NULL)") == "[yes,w,null]"

    def test_undoes_the_steps_before_one_that_fails(self, graph: Database):
        raised = graph.fails(
            f"SELECT drivine_cypher('{self.steps(self.SET, self.INSERT, {'op': 'raise', 'message': 'no'})}', '{{}}')"
        )
        assert isinstance(raised, DrivineError) and str(raised) == "no"
        assert self.count(graph) == 0
        jumped = graph.fails(f"SELECT drivine_cypher('{self.steps(self.SET, self.INSERT, {'op': 'jump'})}', '{{}}')")
        assert "no step jump" in str(jumped)
        assert self.count(graph) == 0
        assert graph.one(f"drivine_cypher('{self.steps(self.SET, self.INSERT, self.RETURNED)}', '{{}}')") == "[1]"
        assert self.count(graph) == 1

    def test_undoes_its_steps_within_a_transaction_and_leaves_the_transaction_open(self, graph: Database):
        graph.connection.execute("BEGIN")
        graph.connection.execute("INSERT INTO cyfunctions_node (labels, props) VALUES ('[]', '{}')")
        graph.fails(
            f"SELECT drivine_cypher('{self.steps(self.SET, self.INSERT, {'op': 'raise', 'message': 'no'})}', '{{}}')"
        )
        assert graph.connection.in_transaction and self.count(graph) == 1
        graph.connection.execute("ROLLBACK")
        assert self.count(graph) == 0
