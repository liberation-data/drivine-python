"""
The SQL a SQLite store is written in, and the functions Drivine registers on each connection: drivine4j's
SqliteGraphTest, SqliteSqlTest, SqliteSchemaGrammarTest and SqliteBackendTest, ported.
"""

import json
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import apsw
import pytest

from drivine import (
    CascadeType,
    DatabaseRegistry,
    DatabaseType,
    DrivineError,
    ModelRegistry,
    NodeFragment,
    NodeId,
    VectorIndex,
)
from drivine.backend.backends import backend_for
from drivine.backend.nearest_search import NearestSearch
from drivine.backend.sqlite import exact_json
from drivine.backend.sqlite.sqlite_backend import SqliteBackend
from drivine.backend.sqlite.sqlite_functions import (
    _delete,
    _lower,
    _Patterns,
    _Similarity,
    epoch,
    register,
)
from drivine.backend.sqlite.sqlite_graph import (
    DELETE,
    SqliteGraph,
    has_label,
    has_labels,
    node_document,
    number_of,
    path,
    text_of,
)
from drivine.backend.sqlite.sqlite_groups import SqliteGroups
from drivine.connection.sqlite.sqlite_session import SqliteSession, bindable
from drivine.model import stamps
from drivine.schema.schema_grammar import schema_grammar_for
from drivine.schema.schema_specs import full_text_index, range_index, uniqueness_constraint, vector_index
from tests.models import PersonCareer


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
        connection = apsw.Connection(":memory:")
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
        # Keys that never give a deleted row's id again (drivine4j#713).
        assert "    nid    INTEGER PRIMARY KEY AUTOINCREMENT,\n" in graph.ddl[0]
        assert "    eid   INTEGER PRIMARY KEY AUTOINCREMENT,\n" in graph.ddl[1]


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
        connection = apsw.Connection(":memory:")
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
        connection = apsw.Connection(":memory:")
        select = f"SELECT {node_document('n')} FROM (SELECT ? AS labels, ? AS props, ? AS stamp) n"
        stamped = connection.execute(select, ('["A"]', '{"x":1}', "s:t")).fetchone()[0]
        assert exact_json.loads(stamped) == {"props": {"x": 1, stamps.PROPERTY: "s:t"}, "labels": ["A"]}
        unstamped = connection.execute(select, ('["A"]', '{"x":1}', None)).fetchone()[0]
        assert exact_json.loads(unstamped) == {"props": {"x": 1}, "labels": ["A"]}

    def test_a_group_key_orders_as_jsonb_does_with_a_node_of_no_value_last_ascending_and_first_descending(self):
        connection = apsw.Connection(":memory:")
        keys = [None, "{}", "[1]", "true", "false", "2.5", "1", '"b"', '"a"', "null"]
        rows = " UNION ALL ".join("SELECT ? AS k" for _ in keys)
        groups = SqliteGroups(SqliteGraph())

        def ordered(ascending: bool) -> list[str | None]:
            order = groups._ordered("g.k", ascending, "KEY")  # pyright: ignore[reportPrivateUsage]
            return [it[0] for it in connection.execute(f"SELECT g.k FROM ({rows}) g ORDER BY {order}", keys)]

        jsonb = ["null", '"a"', '"b"', "1", "2.5", "false", "true", "[1]", "{}", None]
        assert ordered(True) == jsonb
        assert ordered(False) == jsonb[::-1]

    def test_a_named_value_is_bound_as_sqlite_reads_it_and_a_positional_one_as_its_number(self):
        assert bindable("SELECT $a", {"a": {"x": [1]}}) == ("SELECT $a", {"a": '{"x":[1]}'})
        assert bindable("SELECT $1, $10", [1, 2]) == ("SELECT ?1, ?10", (1, 2))


class Doc(NodeFragment, labels=["Doc"]):
    id: Annotated[str, NodeId()]
    embedding: Annotated[list[float], VectorIndex(dimensions=2)] = []


class TestTheStatementsOnAStore:
    """The backend's statements, run on a store in memory with the functions Drivine registers."""

    @staticmethod
    def store(nodes: list[tuple[int, str, str]], edges: list[tuple[int, int]]) -> apsw.Connection:
        connection = apsw.Connection(":memory:")
        for it in SqliteGraph().ddl:
            connection.execute(it)
        connection.execute("PRAGMA foreign_keys = ON")
        register(connection)
        for nid, label, props in nodes:
            connection.execute(
                "INSERT INTO drivine_node (nid, labels, props) VALUES (?, ?, ?)", (nid, f'["{label}"]', props)
            )
        for src, dst in edges:
            connection.execute(
                "INSERT INTO drivine_edge (src, dst, type, props) VALUES (?, ?, 'WORKS_FOR', '{}')", (src, dst)
            )
        return connection

    @pytest.mark.parametrize("cascade", [CascadeType.NONE, CascadeType.DELETE_ALL, CascadeType.DELETE_ORPHAN])
    def test_a_delete_counts_the_roots_alone_and_deletes_once_after_reading_what_it_keeps(self, cascade: CascadeType):
        # Two roots share the id, each employed by an organization of its own; p2 is employed by o2 too, so o2 is no
        # orphan. The count is this library's roots alone; drivine4j counts every node deleted (#375).
        connection = self.store(
            [
                (1, "Person", '{"uuid":"p1","name":"a"}'),
                (2, "Person", '{"uuid":"p1","name":"b"}'),
                (3, "Organization", '{"uuid":"o1","name":"c"}'),
                (4, "Organization", '{"uuid":"o2","name":"d"}'),
                (5, "Person", '{"uuid":"p2","name":"e"}'),
            ],
            [(1, 3), (2, 4), (5, 4)],
        )
        calls: list[object] = []
        connection.create_scalar_function(DELETE, lambda *it: calls.append(it) or _delete(connection, *it), 2)
        statement = SqliteBackend(SqliteGraph()).delete(ModelRegistry().model_of(PersonCareer), None, cascade)

        assert connection.execute(statement.text, {"id": "p1"}).fetchall() == [(2,)]
        assert len(calls) == 1
        left = {CascadeType.NONE: [3, 4, 5], CascadeType.DELETE_ALL: [5], CascadeType.DELETE_ORPHAN: [4, 5]}
        assert [it[0] for it in connection.execute("SELECT nid FROM drivine_node ORDER BY nid")] == left[cascade]

    @pytest.mark.parametrize("dimensions", [2, None])
    def test_a_vector_search_leaves_out_what_has_no_score_with_a_size_or_without(self, dimensions: int | None):
        embeddings = ["[1,0]", "[0,1]", "[1,0,0]", "[0,0]", '"x"']
        connection = self.store(
            [(i, "Doc", f'{{"id":"d{i}","embedding":{it}}}') for i, it in enumerate(embeddings, 1)], []
        )
        search = NearestSearch("Doc", "embedding", "COSINE", 2, dimensions=dimensions)
        statement = SqliteBackend(SqliteGraph()).nearest(ModelRegistry().model_of(Doc), search)
        found = [
            json.loads(it[0]) for it in connection.execute(statement.text, {"_vector": "[1,0]", "_threshold": None})
        ]
        assert [(it["value"]["props"]["id"], it["score"]) for it in found] == [("d1", 1.0), ("d2", 0.5)]


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

    def test_epoch_is_null_wherever_javas_parser_refuses_the_text_and_reads_what_it_takes(self):
        hour = 1_704_103_200_000_000_000  # 2024-01-01T10:00:00Z
        for text in ("2024-01-01t10:00:00z", "2024-01-01T10:00:00z", "2024-01-01T10:00:00.", "2024-01-01T10:00:00.Z"):
            assert epoch(text) == hour
        assert epoch("2024-01-01T10:00:00+01") == hour - 3_600_000_000_000
        assert epoch("2024-01-01T10:00+01") == hour - 3_600_000_000_000
        assert epoch("2024-01-01T10:00:00+18") == hour - 18 * 3_600_000_000_000
        assert epoch("2024-01-01T10:00:00-18:00") == hour + 18 * 3_600_000_000_000
        assert epoch("2024-01-01T10:00:00+01:00:30") == hour - 3_630_000_000_000
        assert epoch("1677-09-21T00:12:44Z") == -9_223_372_036_000_000_000
        assert epoch("2262-04-11T23:47:16.854775807Z") == 2**63 - 1
        for text in (
            "2024-01-01T10:00:00,5Z",
            "2024-01-01T10:00:00+18:30",
            "2024-01-01T10:00:00+25:00",
            "2024-01-01T10:00:00+01:60",
            "2024-01-01T10:00:00.\u0665Z",
            "2024-01-01T10:00:00+\u0660\u0661:00",
            "2024-01-01T10:00:00.0000000001Z",
            "2024-01-01 10:00:00Z",
            "2024-01-01",
            "2024-01-01T10:00:00,5",
            "2024-01-01T10:00.",
            "2024-01-01T10:00:00+0100",
            "2024-01-01T10:00:00+01:00:60",
            "2024-01-01T10:00:00-18:00:01",
            "1677-09-21T00:12:43.145224192Z",
            "1677-09-21T00:12:43.999999999Z",
            "2262-04-11T23:47:16.854775808Z",
        ):
            assert epoch(text) is None, text

    def test_lower_lowers_as_javas_root_locale_does(self):
        assert _lower("\u0130") == "i\u0307"
        assert _lower("\u1e9e") == "\u00df"
        assert _lower("\u03a3\u0391\u03a3") == "\u03c3\u03b1\u03c2"
        assert _lower("\u0391\u03a3.") == "\u03b1\u03c2."
        assert _lower(None) is None

    def test_similarity_scores_as_the_graph_engines_do_and_refuses_what_is_not_a_vector_of_the_size(self):
        similarity = _Similarity()
        assert similarity.score("[1, 0]", "[0, 1]", "COSINE") == pytest.approx(0.5)
        assert similarity.score("[1, 0]", "[1, 0]", "EUCLIDEAN") == pytest.approx(1.0)
        assert similarity.score("[3, 4]", "[0, 0]", "EUCLIDEAN") == pytest.approx(1 / 26)
        for embedding, vector in (("[1]", "[1, 0]"), ('["a", 1]', "[1, 0]"), ("[0, 0]", "[1, 0]"), (None, "[1]")):
            assert similarity.score(embedding, vector, "COSINE") is None
        # A vector with no length has no cosine, and is as near as can be to itself.
        assert similarity.score("[0, 0]", "[0, 0]", "COSINE") is None
        assert similarity.score("[0, 0]", "[0, 0]", "EUCLIDEAN") == pytest.approx(1.0)
        assert similarity.score("[]", "[]", "EUCLIDEAN") == pytest.approx(1.0)
        assert similarity.score("[]", "[]", "COSINE") is None

    def test_a_regular_expression_reads_postgress_posix_classes_and_counts_each_match(self):
        patterns = _Patterns()
        word = "(^|[^[:alnum:]_])école($|[^[:alnum:]_])"
        assert patterns.count("l'école, école", word) == 2
        assert patterns.matches("^a", "abc") == 1 and patterns.matches(None, "x") is None

    def test_a_failure_in_a_function_is_raised_as_itself(self):
        connection = apsw.Connection(":memory:")
        register(connection)
        with pytest.raises(ValueError, match="drivine_write names a graph"):
            connection.execute("SELECT drivine_write('drivine', NULL, NULL, NULL, 'COUNT', NULL)")


class TestContention:
    def test_a_locked_file_or_table_is_contention_by_its_code_or_by_its_message_and_no_other_failure_is(self):
        backend = SqliteBackend()
        busy = apsw.BusyError("database is locked")
        said = apsw.SQLError("database table is locked")
        coded = apsw.SQLError("whatever")
        coded.extendedresult = 517  # SQLITE_BUSY_SNAPSHOT
        wrapped = DrivineError("save failed")
        wrapped.__cause__ = busy
        assert all(backend.contended(it) for it in (busy, said, coded, wrapped, apsw.LockedError("x")))
        assert not backend.contended(apsw.ConstraintError("UNIQUE constraint failed"))
        assert not backend.contended(ValueError("database is locked"))


class TestExactJson:
    def test_keeps_a_numbers_text_and_compares_numbers_by_value_and_a_boolean_only_with_a_boolean(self):
        text = '{"a":1.50,"b":1e3,"c":"ü","d":[true,null],"e":-0,"f":1E+2,"g":0.10e-2}'
        assert exact_json.dumps(exact_json.loads(text)) == text
        assert exact_json.same({"a": Decimal("1.5")}, {"a": Decimal("1.50")})
        assert exact_json.same([1], [Decimal("1.0")])
        assert not exact_json.same(True, 1) and not exact_json.same([0], [False])
        assert not exact_json.same({"a": 1}, {"a": 1, "b": 2})


class TestTheSession:
    def test_a_file_that_cannot_be_opened_is_named_in_the_failure(self, tmp_path: Path):
        missing = str(tmp_path / "no such folder" / "graph.db")
        with pytest.raises(DrivineError, match="no such folder"):
            SqliteSession(missing)

    def test_a_path_that_names_no_file_is_refused_as_each_pooled_connection_would_open_a_database_of_its_own(self):
        for name in (":memory:", "file:graph?mode=memory"):
            with pytest.raises(DrivineError, match="names no file"):
                DatabaseRegistry().builder().database_type(DatabaseType.SQLITE).database_name(name).register("s")

    def test_a_function_that_fails_at_a_later_row_is_raised_as_itself(self, tmp_path: Path):
        session = SqliteSession(str(tmp_path / "graph.db"))
        try:
            session.query("CREATE TABLE t (x TEXT)", {})
            session.query("INSERT INTO t VALUES ('a'), ('b'), ('c')", {})
            with pytest.raises(Exception, match="unterminated") as raised:
                session.query("SELECT CASE WHEN x = 'b' THEN x REGEXP '(' END AS r FROM t ORDER BY x", {})
            assert not isinstance(raised.value, apsw.Error)
        finally:
            session.close()

    def test_floats_are_written_to_17_digits_and_a_column_named_where_a_statement_gives_no_row(self, tmp_path: Path):
        # apsw bundles SQLite 3.53 or later, which writes a float as text to 17 significant digits (#376).
        session = SqliteSession(str(tmp_path / "graph.db"))
        try:
            assert session.query("SELECT json_quote(0.1 + 0.2) AS f, json_array(1.0 / 3) AS t", {}) == [
                {"f": "0.30000000000000004", "t": "[0.33333333333333332]"}
            ]
            assert session.query("SELECT 1 AS n WHERE 0", {}) == []
            rows = session.execute("SELECT 1 AS n UNION ALL SELECT 2", ())
            try:
                assert session.fetch("", rows, 1) == [{"n": 1}]
                assert session.fetch("", rows, 5) == [{"n": 2}]
                assert session.fetch("", rows, 5) == []
            finally:
                rows.close()
        finally:
            session.close()

    def test_a_sqlite_older_than_3_53_is_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(apsw, "sqlite_lib_version", lambda: "3.49.1")
        with pytest.raises(DrivineError, match="SQLite 3.53 or later, and apsw runs SQLite 3.49.1"):
            SqliteSession(str(tmp_path / "graph.db"))
