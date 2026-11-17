"""
The functions compiled Cypher calls on SQLite that drivine-python registers itself, each asked directly in SQL: what
each gives, and what it gives of null, as drivine4j's `SqliteCypherFunctionsTest` asks them. The shared module's, which
its SQLite extension registers, host.yaml pins (test_host_spec) and the module's own tests.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterator
from typing import Any

import apsw
import pytest

from drivine.backend.sqlite import sqlite_graph
from drivine.backend.sqlite.sqlite_functions import register
from drivine.drivine_error import DrivineError


class Database:
    def __init__(self) -> None:
        self.connection = apsw.Connection(":memory:")
        register(self.connection)

    def row(self, sql: str) -> tuple[Any, ...]:
        return self.connection.execute(sql).fetchone()

    def one(self, sql: str) -> Any:
        return self.row(f"SELECT {sql}")[0]

    def fails(self, sql: str) -> BaseException:
        """The failure a function raised in the statement, which fails the statement as itself."""
        with pytest.raises(Exception) as raised:
            self.row(sql)
        assert not isinstance(raised.value, apsw.Error)
        return raised.value


@pytest.fixture
def db() -> Iterator[Database]:
    database = Database()
    yield database
    database.connection.close()


def test_each_function_is_named_as_drivine4j_names_it():
    names = [
        sqlite_graph.EQUAL, sqlite_graph.ADD, sqlite_graph.CANONICAL, sqlite_graph.CYPHER,
        sqlite_graph.UPPER, sqlite_graph.SPLIT, sqlite_graph.REVERSE, sqlite_graph.UUID, sqlite_graph.REGEX_GROUP,
        sqlite_graph.REGEX_REPLACE, sqlite_graph.TRUNC, sqlite_graph.TO_CHAR, sqlite_graph.TEMPORAL,
        sqlite_graph.FLOAT_TEXT, sqlite_graph.SORT_KEY, sqlite_graph.LIST_ORDER, sqlite_graph.MATH, sqlite_graph.ROUND,
        sqlite_graph.FAIL,
    ]  # fmt: skip
    assert names == [
        "drivine_equal", "drivine_add", "drivine_canonical", "drivine_cypher", "drivine_upper",
        "drivine_split", "drivine_reverse", "drivine_uuid", "drivine_regex_group", "drivine_regexp_replace",
        "drivine_trunc", "drivine_to_char", "drivine_temporal", "drivine_float_text", "drivine_sort_key",
        "drivine_list_order", "drivine_math", "drivine_round", "drivine_fail",
    ]  # fmt: skip


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

    def test_keeps_rows_in_a_list_a_step_reads_as_json_and_fails_to_keep_one_in_a_list_not_made(self, graph: Database):
        # A row kept is the text, or the number, its step gave: a real one as Java writes a double, a null as null.
        keep = {
            "op": "keep",
            "sql": "SELECT json_object('k', ?1)",
            "variables": ["cy_record.item"],
            "variable": "cy_out",
        }
        program = self.steps(
            {"op": "kept", "variable": "cy_out"},
            {
                "op": "loop",
                "sql": "SELECT value FROM json_each('[1, 2]')",
                "variables": [],
                "record": "cy_record.item",
                "body": [keep],
            },
            {"op": "keep", "sql": "SELECT 1e20", "variables": [], "variable": "cy_out"},
            {"op": "keep", "sql": "SELECT NULL", "variables": [], "variable": "cy_out"},
            {"op": "select", "sql": "SELECT ?1", "variables": ["cy_out"], "variable": "cy_rows"},
            {"op": "return", "sql": "SELECT json_array_length(?1)", "variables": ["cy_rows"]},
            {"op": "return", "sql": "SELECT ?1", "variables": ["cy_out"]},
        )
        assert graph.one(f"drivine_cypher('{program}', '{{}}')") == '[4,[{"k":1},{"k":2},1.0E20,null]]'
        unmade = graph.fails(
            f"SELECT drivine_cypher('{self.steps({'op': 'keep', 'sql': 'SELECT 1', 'variable': 'cy_out'})}', '{{}}')"
        )
        assert str(unmade) == "drivine_cypher keeps no rows in cy_out."

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
