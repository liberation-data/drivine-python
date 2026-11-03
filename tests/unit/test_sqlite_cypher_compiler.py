"""
The SQL a Cypher statement compiles to on SQLite, as drivine4j's SqliteCypherCompiler writes it: a statement that writes
is steps `drivine_cypher` carries out, a lateral query is gathered as a JSON list, and nulls order where Cypher puts
them. What the statements give is the Cypher spec's, run on SQLite in the integration tests.
"""

import json
import re
import sqlite3
from typing import Any

import pytest

from drivine.backend.sqlite.sqlite_functions import Failures, register
from drivine.backend.sqlite.sqlite_graph import SqliteGraph
from drivine.connection.sqlite.sqlite_session import bindable
from drivine.cypher.sql.oracle_cypher_compiler import OracleCypherCompiler
from drivine.cypher.sql.postgres_cypher_compiler import PostgresCypherCompiler
from drivine.cypher.sql.sqlite_cypher_compiler import SqliteCypherCompiler, _step_sql


def steps(cypher: str) -> list[dict[str, Any]]:
    """The steps a statement that writes compiles to on SQLite, as `drivine_cypher` reads them."""
    sql = SqliteCypherCompiler("g").compile(cypher, {}).sql
    literal = re.search(r"drivine_cypher\('((?:[^']|'')*)', \$_cy_params\)", sql)
    assert literal is not None, sql
    return json.loads(literal[1].replace("''", "'"))["steps"]


def test_a_writes_variables_are_numbered_parameters_and_a_literal_that_names_one_is_as_it_is() -> None:
    program = steps("CREATE (n:Person {name: 'cy_row'}) RETURN n.name")
    create = next(it for it in program if it["op"] == "loop")
    assert create["record"] == "cy_record.item"
    insert = next(it for it in create["body"] if it["op"] == "execute")
    assert insert["variable"] == "cy_id"
    assert "RETURNING nid" in insert["sql"]
    # Each variable the step reads is a parameter of its own, numbered in the order it reads them.
    for i, _ in enumerate(insert["variables"]):
        assert f"?{i + 1}" in insert["sql"]
    assert "cy_value" in insert["variables"]
    # A text the statement writes that spells a variable is no variable.
    value = next(it for it in create["body"] if it["op"] == "select" and it["variable"] == "cy_value")
    assert "cy_row" in value["sql"]
    assert "cy_value" not in value["variables"]
    assert program[-1]["op"] == "return"


def test_a_records_item_is_one_parameter_and_a_quoted_name_is_read_past() -> None:
    step = _step_sql("select", 'SELECT cy_record.item, "cy_rows", cy_rows, x.cy_rows, cy_rowsx FROM t')
    assert step["sql"] == 'SELECT ?1, "cy_rows", ?2, x.cy_rows, cy_rowsx FROM t'
    assert step["variables"] == ["cy_record.item", "cy_rows"]


def test_a_lateral_query_is_gathered_as_a_json_list_read_back_by_its_columns() -> None:
    sql = (
        SqliteCypherCompiler("g")
        .compile("MATCH (a:Person) OPTIONAL MATCH (a)-[:KNOWS]->(b) RETURN a.name, b.name", {})
        .sql
    )
    assert "LEFT JOIN json_each((SELECT json_group_array(json_object(" in sql
    assert re.search(r"NULLIF\(o\d+\.value -> '\$\.v\d+_props', 'null'\)", sql)
    assert "LATERAL" not in sql


def test_nulls_order_where_cypher_puts_them_and_a_page_is_sqlites_limit() -> None:
    sql = SqliteCypherCompiler("g").compile("MATCH (a:Person) RETURN a.name ORDER BY a.name DESC SKIP 1", {}).sql
    assert "DESC NULLS FIRST" in sql
    assert "LIMIT -1 OFFSET 1" in sql


def test_a_float_the_statement_writes_is_written_as_java_writes_a_double() -> None:
    sql = SqliteCypherCompiler("g").compile("RETURN [1.0E9, 0.5, 1]", {}).sql
    assert "'1.0E9'" in sql
    assert "'0.5'" in sql


def test_a_statement_reads_one_clock() -> None:
    sql = SqliteCypherCompiler("g").compile("RETURN [datetime(), datetime(), localdatetime()]", {}).sql
    clocks = set(re.findall(r"'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}'", sql))
    assert len(clocks) == 1, clocks


def test_the_last_item_and_a_negative_index_are_read_by_a_path_sqlite_before_3_47_takes() -> None:
    # SQLite before 3.47 refuses `-> -1`; the path `$[#-1]` it takes (#377).
    sql = SqliteCypherCompiler("g").compile("WITH [1, 2, 3] AS l RETURN [last(l), l[-2], l[$i]]", {"i": -1}).sql
    assert "-> '$[#-1]'" in sql
    assert sql.count("replace(CAST(") == 2
    assert not re.search(r"-> *(-|CAST)", sql)


def plan(cypher: str) -> list[str]:
    """The tables SQLite reads for the statement, in the order its plan reads them: each `SCAN` and `SEARCH`."""
    connection = sqlite3.connect(":memory:")
    register(connection, Failures())
    for statement in SqliteGraph("g").ddl:
        connection.execute(statement)
    sql, values = bindable(SqliteCypherCompiler("g").compile(cypher, {}).sql, {})
    details = [row[3] for row in connection.execute(f"EXPLAIN QUERY PLAN {sql}", values)]
    connection.close()
    return [it for it in details if it.startswith(("SCAN ", "SEARCH "))]


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (n)-[r]->(m) RETURN count(*)",
        "MATCH (n)-[r]-(m) RETURN count(*)",
        "MATCH (n)-[*1..2]-(m) RETURN count(*)",
    ],
)
def test_sqlite_may_reorder_a_patterns_sources_so_a_step_reads_no_pair_of_nodes_before_a_relationship(
    cypher: str,
) -> None:
    # Across a CROSS JOIN SQLite reads the tables in the order written: SCAN n1, SCAN n2, then the relationships, every
    # pair of nodes (#391). Joined with JOIN … ON TRUE, it reads a node table in full once at most before an edge.
    sql = SqliteCypherCompiler("g").compile(cypher, {}).sql
    assert "CROSS JOIN" not in sql
    read = plan(cypher)
    edge = next(i for i, it in enumerate(read) if re.match(r"(SCAN|SEARCH) e\d+\b", it))
    scans = [it for it in read[:edge] if re.fullmatch(r"SCAN n\d+", it)]
    assert len(scans) <= 1, read


def test_a_json_each_over_an_earlier_sources_column_is_read_after_it() -> None:
    # A plain join leaves SQLite free to reorder; a json_each that reads n1's column is still read after n1.
    read = plan("MATCH (n) UNWIND n.tags AS t RETURN t")
    nodes = next(i for i, it in enumerate(read) if re.fullmatch(r"SCAN n\d+", it))
    items = next(i for i, it in enumerate(read) if it.startswith("SCAN u"))
    assert nodes < items, read


def test_a_lateral_query_joins_on_true_on_sqlite_where_postgres_and_oracle_keep_a_cross_join() -> None:
    # A CALL that imports its row is a lateral query; SQLite joins its json_each as it joins any source.
    called = SqliteCypherCompiler("g").compile("MATCH (a:P) CALL { WITH a MATCH (a)-->(b) RETURN b } RETURN a, b", {})
    assert "JOIN json_each((SELECT json_group_array(" in called.sql
    assert "CROSS JOIN" not in called.sql
    # Postgres and Oracle reorder a CROSS JOIN as they do any join, and their SQL is as it was.
    step = "MATCH (n)-[r]->(m) RETURN n, r, m"
    for compiler in (PostgresCypherCompiler("g"), OracleCypherCompiler("g")):
        sql = compiler.compile(step, {}).sql
        assert "FROM g_node n1\n    CROSS JOIN g_node n2\n    CROSS JOIN g_edge e3" in sql
        assert " ON TRUE" not in sql
