"""
The SQL a SQLite store is written in, and the functions Drivine registers on each connection: drivine4j's
SqliteGraphTest, SqliteSqlTest, SqliteSchemaGrammarTest and SqliteBackendTest, ported.
"""

import sqlite3
from decimal import Decimal

import pytest

from drivine import DatabaseType, DrivineError
from drivine.backend.backends import backend_for
from drivine.backend.sqlite import exact_json
from drivine.backend.sqlite.sqlite_backend import SqliteBackend
from drivine.backend.sqlite.sqlite_functions import Failures, _Patterns, _Similarity, epoch, register
from drivine.backend.sqlite.sqlite_graph import (
    SqliteGraph,
    has_label,
    has_labels,
    node_document,
    number_of,
    path,
    text_of,
)
from drivine.connection.sqlite.sqlite_session import bindable
from drivine.model import stamps
from drivine.schema.schema_grammar import schema_grammar_for
from drivine.schema.schema_specs import full_text_index, range_index, uniqueness_constraint, vector_index


class TestTheGraph:
    def test_names_its_tables_after_itself_as_oracles_and_postgress_do(self):
        graph = SqliteGraph("Contracts")
        assert (graph.nodes, graph.edges, graph.catalog) == ("Contracts_node", "Contracts_edge", "Contracts_schema")
        assert isinstance(backend_for(DatabaseType.SQLITE, "g"), SqliteBackend)

    def test_refuses_a_name_no_table_can_be_named_after(self):
        for name in ("1st", "has space", "x" * 60, "semi;colon"):
            with pytest.raises(DrivineError, match="SQLite"):
                SqliteGraph(name)

    def test_is_the_tables_postgres_keeps_with_json_as_text_each_statement_safe_to_run_again(self):
        connection = sqlite3.connect(":memory:")
        graph = SqliteGraph()
        for _ in range(2):
            for it in graph.ddl:
                assert "IF NOT EXISTS" in it
                connection.execute(it)
        columns = {
            table: [(it[1], it[2]) for it in connection.execute(f"PRAGMA table_info({table})")]
            for table in (graph.nodes, graph.edges)
        }
        assert columns == {
            "drivine_node": [("nid", "INTEGER"), ("labels", "TEXT"), ("props", "TEXT"), ("stamp", "TEXT")],
            "drivine_edge": [
                ("eid", "INTEGER"),
                ("src", "INTEGER"),
                ("dst", "INTEGER"),
                ("type", "TEXT"),
                ("props", "TEXT"),
            ],
        }
        assert connection.execute(graph.made).fetchone() == (len(graph.ddl),)


class TestTheSql:
    def test_a_label_is_a_test_of_the_labels_text_for_the_label_as_json_writes_it_escaped_for_json_and_sql(self):
        assert has_label("n", "Person") == "instr(n.labels, '\"Person\"') > 0"
        assert has_label("n", 'It\'s "x"') == "instr(n.labels, '\"It''s \\\"x\\\"\"') > 0"
        assert has_labels("n", []) == "1 = 1"
        assert has_label("", "L") == "instr(labels, '\"L\"') > 0"

    def test_a_property_is_read_by_a_quoted_path_so_a_dotted_name_is_one_property(self):
        assert path("meta.source") == "'$.\"meta.source\"'"
        with pytest.raises(DrivineError, match="empty"):
            path("")

    def test_a_property_is_read_as_the_text_postgres_gives_a_number_only_where_one_is_stored(self):
        connection = sqlite3.connect(":memory:")
        props = '{"s":"7","i":7,"r":1.50,"t":true,"z":null,"a":[1,"x"]}'
        read = {
            key: connection.execute(
                f"SELECT {text_of('n', key)}, {number_of('n', key)} FROM (SELECT ? AS props) n", (props,)
            ).fetchone()
            for key in ("s", "i", "r", "t", "z", "a", "missing")
        }
        assert read == {
            "s": ("7", None),
            "i": ("7", 7),
            "r": ("1.50", 1.5),
            "t": ("true", None),
            "z": (None, None),
            "a": ('[1,"x"]', None),
            "missing": (None, None),
        }
        assert text_of("n", stamps.PROPERTY) == "n.stamp"

    def test_a_nodes_document_is_its_properties_with_its_stamp_and_its_labels_apart(self):
        connection = sqlite3.connect(":memory:")
        select = f"SELECT {node_document('n')} FROM (SELECT ? AS labels, ? AS props, ? AS stamp) n"
        stamped = connection.execute(select, ('["A"]', '{"x":1}', "s:t")).fetchone()[0]
        assert exact_json.loads(stamped) == {"props": {"x": 1, stamps.PROPERTY: "s:t"}, "labels": ["A"]}
        unstamped = connection.execute(select, ('["A"]', '{"x":1}', None)).fetchone()[0]
        assert exact_json.loads(unstamped) == {"props": {"x": 1}, "labels": ["A"]}

    def test_a_named_value_is_bound_as_sqlite_reads_it_and_a_positional_one_as_its_number(self):
        assert bindable("SELECT $a", {"a": {"x": [1]}}) == ("SELECT $a", {"a": '{"x":[1]}'})
        assert bindable("SELECT $1, $10", [1, 2]) == ("SELECT ?1, ?10", (1, 2))


class TestTheSchemaGrammar:
    grammar = schema_grammar_for(DatabaseType.SQLITE)

    def test_a_range_index_is_a_partial_index_over_the_property_as_a_statement_reads_it_for_its_label(self):
        [made, recorded] = self.grammar.create_range(range_index("Person", "name"))
        assert made.startswith("CREATE INDEX IF NOT EXISTS drivine_ix_Personname_")
        assert made.endswith(f"ON drivine_node ({text_of('', 'name')}) WHERE {has_label('', 'Person')}")
        assert "INSERT INTO drivine_schema" in recorded and "'RANGE_INDEX'" in recorded and "'[\"name\"]'" in recorded

    def test_a_uniqueness_constraint_is_that_index_made_unique(self):
        [made, _] = self.grammar.create_unique(uniqueness_constraint("Person", "email"))
        assert made.startswith("CREATE UNIQUE INDEX IF NOT EXISTS drivine_uq_Personemail_")

    def test_a_vector_and_a_full_text_index_are_recorded_and_nothing_is_made_for_them(self):
        [vector] = self.grammar.create_vector(vector_index("Doc", "embedding", 3))
        [text] = self.grammar.create_full_text(full_text_index("Doc", ["body"]))
        assert vector.startswith("INSERT INTO drivine_schema") and '"dimensions": 3' in vector
        assert text.startswith("INSERT INTO drivine_schema") and "NULL" in text

    def test_an_index_is_dropped_by_the_name_its_kind_gives_it_and_one_never_made_is_only_forgotten(self):
        [dropped, forgotten] = self.grammar.drop_range(range_index("Person", "name"))
        assert dropped.startswith("DROP INDEX IF EXISTS drivine_ix_Personname_")
        assert forgotten.startswith("DELETE FROM drivine_schema WHERE kind = 'RANGE_INDEX'")
        [only] = self.grammar.drop_vector(vector_index("Doc", "embedding", 3))
        assert only.startswith("DELETE FROM drivine_schema")


class TestTheFunctions:
    def test_epoch_reads_a_moment_to_the_nanosecond_its_offset_taken_and_none_as_utc(self):
        assert epoch("1970-01-01T00:00:00Z") == 0
        assert epoch("1970-01-01T00:00:01.000000001Z") == 1_000_000_001
        assert epoch("1970-01-01T01:00:00+01:00") == 0
        assert epoch("1970-01-01T00:00:00.5") == 500_000_000
        assert epoch("1969-12-31T23:59:59.999999999Z") == -1
        assert epoch("2024-05-06T07:08:09.123456Z") < epoch("2024-05-06T09:08:09.123457+02:00")  # type: ignore[operator]
        for text in ("not a moment", "2024-13-01T00:00:00Z", None, 7):
            assert epoch(text) is None

    def test_similarity_scores_as_the_graph_engines_do_and_refuses_what_is_not_a_vector_of_the_size(self):
        similarity = _Similarity()
        assert similarity.score("[1, 0]", "[0, 1]", "COSINE") == pytest.approx(0.5)
        assert similarity.score("[1, 0]", "[1, 0]", "EUCLIDEAN") == pytest.approx(1.0)
        assert similarity.score("[3, 4]", "[0, 0]", "EUCLIDEAN") == pytest.approx(1 / 26)
        for embedding, vector in (("[1]", "[1, 0]"), ('["a", 1]', "[1, 0]"), ("[0, 0]", "[1, 0]"), (None, "[1]")):
            assert similarity.score(embedding, vector, "COSINE") is None

    def test_a_regular_expression_reads_postgress_posix_classes_and_counts_each_match(self):
        patterns = _Patterns()
        word = "(^|[^[:alnum:]_])école($|[^[:alnum:]_])"
        assert patterns.count("l'école, école", word) == 2
        assert patterns.matches("^a", "abc") == 1 and patterns.matches(None, "x") is None

    def test_a_failure_in_a_function_is_kept_and_raised_as_itself(self):
        connection = sqlite3.connect(":memory:")
        failures = Failures()
        register(connection, failures)
        with pytest.raises(sqlite3.OperationalError, match="user-defined function raised exception"):
            connection.execute("SELECT drivine_write('drivine', NULL, NULL, NULL, 'COUNT', NULL)")
        assert isinstance(failures.take(), ValueError)
        assert failures.take() is None


class TestContention:
    def test_a_locked_file_or_table_is_contention_by_its_code_or_by_its_message_and_no_other_failure_is(self):
        backend = SqliteBackend()
        busy = sqlite3.OperationalError("database is locked")
        coded = sqlite3.OperationalError("whatever")
        coded.sqlite_errorcode = 517  # SQLITE_BUSY_SNAPSHOT
        wrapped = DrivineError("save failed")
        wrapped.__cause__ = busy
        assert backend.contended(busy) and backend.contended(coded) and backend.contended(wrapped)
        assert not backend.contended(sqlite3.IntegrityError("UNIQUE constraint failed"))
        assert not backend.contended(ValueError("database is locked"))


class TestExactJson:
    def test_keeps_a_numbers_text_and_compares_numbers_by_value_and_a_boolean_only_with_a_boolean(self):
        text = '{"a":1.50,"b":1e3,"c":"ü","d":[true,null],"e":-0,"f":1E+2,"g":0.10e-2}'
        assert exact_json.dumps(exact_json.loads(text)) == text
        assert exact_json.same({"a": Decimal("1.5")}, {"a": Decimal("1.50")})
        assert exact_json.same([1], [Decimal("1.0")])
        assert not exact_json.same(True, 1) and not exact_json.same([0], [False])
        assert not exact_json.same({"a": 1}, {"a": 1, "b": 2})
