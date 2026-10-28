"""
On Oracle, `reverse()` reverses a text as a `VARCHAR2` with `LISTAGG`, and where that gives null, a text past 4,000
bytes, reads it as a CLOB and reverses it in pieces, put together as JSON (#182). drivine4j's OracleReverseTextTest,
ported.
"""

import re

from drivine.cypher.sql.oracle_cypher_compiler import OracleCypherCompiler
from drivine.cypher.sql.postgres_cypher_compiler import PostgresCypherCompiler


def oracle(cypher: str) -> str:
    return OracleCypherCompiler("g").compile(cypher, {"t": "abc"}).sql


def test_a_text_is_reversed_as_a_varchar2_first_and_where_that_gives_null_as_a_clob_in_pieces_put_together_as_json():
    sql = oracle("WITH $t AS t RETURN reverse(t)")
    short = "COALESCE(JSON_SCALAR((SELECT LISTAGG(SUBSTR("
    assert short in sql, sql
    long = sql.split(short, 1)[1]
    # The CLOB, bound once, cut at each 1,000th UTF-16 unit, or one past it where a character past U+FFFF starts there.
    assert "TO_CLOB(CASE WHEN" in long and "JSON_VALUE(w2.c1, '$' RETURNING CLOB) END) AS v1" in long, long
    assert "CONNECT BY LEVEL <= CEIL(LENGTH(sh" in long, long
    assert "CASE WHEN LENGTH(REGEXP_SUBSTR(sh" in long, long
    assert ".v1, '.', LEVEL * 1000, 1, 'n')) = 2 THEN 1 ELSE 0 END" in long, long
    assert "DBMS_LOB.SUBSTR(sh" in long, long
    # Each piece reversed as a short text is, and the pieces put together last to first, written in ASCII and read as
    # JSON.
    assert (
        "(SELECT LISTAGG(SUBSTR(rv_piece.s, LEVEL, 1)) WITHIN GROUP (ORDER BY LEVEL DESC) FROM DUAL "
        "CONNECT BY LEVEL <= LENGTH(rv_piece.s)) r" in long
    ), long
    joined = (
        "JSON_SERIALIZE(JSON_ARRAYAGG('x' || rv_reversed.r ORDER BY rv_reversed.n DESC RETURNING JSON) "
        "RETURNING CLOB ASCII)"
    )
    assert (
        f"""(SELECT JSON_QUERY(JSON(REPLACE(REPLACE({joined}, '","x'), '["x', '["')), '$[0]' RETURNING JSON)""" in long
    ), long
    # Worked out once, and not made JSON again.
    assert len(re.findall(r"JSON_ARRAYAGG\('x'", sql)) == 1, sql
    assert "JSON_SCALAR(CASE WHEN LENGTH((SELECT JSON_QUERY" not in long, long


def test_a_stored_text_reversed_is_read_by_its_own_path_and_on_postgres_a_text_is_reversed_as_it_was():
    stored = oracle("MATCH (n:Doc) RETURN reverse(n.a)")
    assert "JSON_VALUE(n1.props, '$.a')" in stored and "JSON_VALUE(n1.props, '$.a' RETURNING CLOB)" in stored, stored
    postgres = PostgresCypherCompiler("g").compile("WITH $t AS t RETURN reverse(t)", {"t": "abc"}).sql
    assert "THEN to_jsonb(reverse(CASE WHEN jsonb_typeof(w2.c1) = 'string' THEN w2.c1 #>> '{}' END))" in postgres, (
        postgres
    )
