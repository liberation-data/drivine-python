"""
On Oracle, a query that reads the rows of a `CALL { … }` or a `FOREACH` written for each row alone is planned for one
row of them, `CARDINALITY(r 1)` in its own query's hint: Oracle estimated 8,168 rows of the `JSON_TABLE`, and read every
node by a hash join for each row (#419, liberation-data/drivine4j#588). A query of the statement's rows, before or after
the loop, is planned as it was. drivine4j's `OracleOneRowPlanTest`, and Drivine for TypeScript's
`OracleOneRowPlan.spec.ts`.
"""

import re

import pytest

from drivine.cypher.sql.oracle_cypher_compiler import OracleCypherCompiler


def compiled(cypher: str) -> str:
    return OracleCypherCompiler("g").compile(cypher).sql


def hinted(sql: str) -> list[str]:
    """The aliases of the rows each hinted query is told hold one."""
    return re.findall(r"SELECT /\*\+ [^*]*CARDINALITY\((r\d+) 1\)[^*]*\*/", sql)


PROJECTION = re.compile(
    r"SELECT /\*\+ CARDINALITY\((r\d+) 1\) \*/ JSON\(JSON_QUERY\(JSON_SERIALIZE\(JSON_TRANSFORM\(n\d+\.props, "
    r"KEEP '\$\.i'\)[^\n]*\n\s*r\d+\.item_no AS o1\n\s*FROM JSON_TABLE\(cy_rows, '\$\[\*\]' COLUMNS \(item_no FOR "
    r"ORDINALITY, item JSON PATH '\$'\)\) (r\d+)\n"
)
"""The query that projects what a `CALL (n) { … RETURN n.i AS j }` returns for its row, hinted for that row's alias."""


def test_a_calls_projection_of_each_row_reads_it_as_one_row_in_the_query_that_reads_it():
    sql = compiled("MATCH (n:CyLoop) CALL (n) { SET n.y = n.i RETURN n.i AS j } RETURN count(j)")
    found = PROJECTION.search(sql)
    assert found is not None and found.group(1) == found.group(2), sql
    # The loop over the call's rows, and the query of all the statement's rows after it, read them as before.
    assert "FOR cy_record IN (SELECT r2.item FROM JSON_TABLE(cy_saved1," in sql, sql
    returned = sql[sql.index("OPEN cy_cursor FOR") :]
    assert re.search(r"\n\s*SELECT [^\n]*\bp\d+\.c1\b[^\n]* AS result\n", returned), sql
    assert "CARDINALITY" not in returned, sql
    assert len(set(hinted(sql))) == len(hinted(sql)), sql


def test_a_text_before_the_rows_read_with_a_bracket_a_quote_or_select_in_it_is_read_past_to_the_querys_own_select():
    plain = hinted(compiled("MATCH (n:CyLoop) CALL (n) { SET n.y = n.i RETURN toString(n.i) AS j } RETURN j"))
    sql = compiled("MATCH (n:CyLoop) CALL (n) { SET n.y = n.i RETURN toString(n.i) + ') SELECT \\'(' AS j } RETURN j")
    assert ") SELECT ''(" in sql, sql
    assert plain and len(hinted(sql)) == len(plain), sql


def test_a_foreachs_rows_read_alone_are_one_row():
    assert hinted(compiled("UNWIND [1, 2] AS k FOREACH (x IN [k] | CREATE (:CyWrite {f: x})) RETURN k")), "FOREACH"


@pytest.mark.parametrize(
    "cypher",
    [
        "UNWIND range(1, 3) AS i CREATE (:CyLoop {i: i})",
        "MATCH (n:CyLoop) SET n.x = n.i RETURN n.x",
        "MATCH (n:CyLoop) RETURN n.i",
    ],
)
def test_a_statement_with_neither_a_call_nor_a_foreach_is_planned_as_it_was(cypher: str):
    assert "CARDINALITY" not in compiled(cypher), cypher


def test_a_query_of_the_rows_alone_that_has_a_hint_of_its_own_is_told_in_it():
    compiler = OracleCypherCompiler("g")
    compiler._one_row.append("r1")
    read = "FROM JSON_TABLE(cy_rows, '$[*]' COLUMNS (item_no FOR ORDINALITY, item JSON PATH '$')) r1"
    planned = compiler._one_row_planned(f"SELECT x FROM (SELECT /*+ NO_MERGE */ r1.item {read}) t")
    assert planned == f"SELECT x FROM (SELECT /*+ NO_MERGE CARDINALITY(r1 1) */ r1.item {read}) t"


@pytest.mark.parametrize(
    "query",
    [
        "(FROM JSON_TABLE(cy_rows, '$[*]' COLUMNS (item_no FOR ORDINALITY, item JSON PATH '$')) r1",
        "x' FROM JSON_TABLE(cy_rows, '$[*]' COLUMNS (item_no FOR ORDINALITY, item JSON PATH '$')) r1",
        "RESELECT x FROM JSON_TABLE(cy_rows, '$[*]' COLUMNS (item_no FOR ORDINALITY, item JSON PATH '$')) r1",
    ],
)
def test_rows_alone_read_by_no_select_of_their_own_leave_the_query_as_it_was(query: str):
    # A bracket opened before the read, a quote not closed, or SELECT only as the end of a name: no query to hint.
    compiler = OracleCypherCompiler("g")
    compiler._one_row.append("r1")
    assert compiler._one_row_planned(query) == query
