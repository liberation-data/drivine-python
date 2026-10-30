"""
On Oracle, CONTAINS looks for a text with `INSTR`, as `VARCHAR2`s first, and where that gives null as CLOBs. `INSTR`
refuses to look for a text past 32,767 UTF-16 units (ORA-22828), so a longer one is compared with `DBMS_LOB.COMPARE`
where it is as long as the text, and else at each place its first 1,000 units are, carried from one to the next by
`MODEL` (drivine4j#312). drivine4j's OracleContainsTextTest, ported.
"""

import re
from typing import Any

from drivine.cypher.sql.oracle_cypher_compiler import OracleCypherCompiler
from drivine.cypher.sql.postgres_cypher_compiler import PostgresCypherCompiler

GIVEN = "WITH $t AS t, $x AS x RETURN t CONTAINS x"


def oracle(cypher: str, parameters: dict[str, Any] | None = None) -> str:
    return OracleCypherCompiler("g").compile(cypher, {"t": "abc", "x": "b"} if parameters is None else parameters).sql


def test_a_text_is_looked_for_by_instr_as_a_varchar2_and_as_a_clob_by_instr_dbms_lob_compare_or_by_its_first_units():
    sql = oracle(GIVEN)
    short = "COALESCE((INSTR(CASE WHEN"
    assert short in sql, sql
    long = sql.split(short, 1)[1].split("(SELECT (CASE WHEN ", 1)[1]
    bound = re.match(r"^(sh\d+\.v1) IS NULL OR (sh\d+\.v2) IS NULL THEN NULL ", long)
    assert bound, long
    t, x = bound.groups()
    # The CLOBs, bound once.
    assert "JSON_VALUE(w2.c1, '$' RETURNING CLOB) END AS v1" in long, long
    # Each a number, and only the CASE compared with 1: Oracle found (SELECT … MODEL …) = 1 false where the subquery
    # gave 1.
    assert f"WHEN LENGTH({x}) <= 32767 THEN SIGN(INSTR({t}, {x})) " in long, long
    assert (
        f"WHEN LENGTH({x}) >= LENGTH({t}) THEN CASE WHEN LENGTH({x}) = LENGTH({t}) "
        f"AND DBMS_LOB.COMPARE({t}, {x}) = 0 THEN 1 ELSE 0 END "
    ) in long, long
    search = (
        f"ELSE (SELECT cs_found FROM DUAL MODEL DIMENSION BY (0 cs_i) "
        f"MEASURES (INSTR({t}, SUBSTR({x}, 1, 1000)) cs_at, 0 cs_found) "
        f"RULES ITERATE (4294967295) UNTIL (cs_found[0] <> 0) "
        f"(cs_found[0] = CASE WHEN cs_at[0] = 0 OR cs_at[0] > LENGTH({t}) - LENGTH({x}) + 1 THEN -1 "
        f"WHEN DBMS_LOB.COMPARE({t}, {x}, LENGTH({x}), cs_at[0], 1) = 0 THEN 1 ELSE 0 END, "
        f"cs_at[0] = CASE WHEN cs_found[0] = 0 THEN INSTR({t}, SUBSTR({x}, 1, 1000), cs_at[0] + 1) "
        f"ELSE cs_at[0] END)) END = 1)"
    )
    assert search in long, long
    # A short text's VARCHAR2 is looked for as before, and only by INSTR.
    assert sql.count("MODEL DIMENSION BY") == 1, sql
    assert "DBMS_LOB" not in sql.split(", (SELECT (CASE WHEN", 1)[0], sql


def test_a_stored_text_is_bound_by_its_own_path_for_its_clob_and_on_postgres_a_text_is_looked_for_as_it_was():
    stored = oracle("MATCH (n:Doc) RETURN n.t CONTAINS n.x")
    # Read inside the bound subquery, as drivine4j#370 has a property beside one read.
    assert "JSON_VALUE(n1.props, '$.t' RETURNING CLOB) END AS v1" in stored, stored
    assert "JSON_VALUE(n1.props, '$.x' RETURNING CLOB) END AS v2" in stored, stored
    assert (
        "(INSTR(CASE WHEN JSON_VALUE(n1.props, '$.t.type()') = 'string' THEN JSON_VALUE(n1.props, '$.t') END"
    ) in stored, stored
    postgres = PostgresCypherCompiler("g").compile(GIVEN, {"t": "abc", "x": "b"}).sql
    assert "strpos(" in postgres and "MODEL" not in postgres, postgres


def test_contains_in_a_write_a_where_and_beside_a_long_value_takes_the_same_search():
    written = oracle("MATCH (n:Doc) SET n.r = n.t CONTAINS $x", {"x": "b"})
    assert "MODEL DIMENSION BY (0 cs_i)" in written, written
    filtered = oracle("MATCH (n:Doc) WHERE n.t CONTAINS $x RETURN n")
    assert "MODEL DIMENSION BY (0 cs_i)" in filtered, filtered
    beside = oracle(
        "MATCH (n:Doc), (m:Doc) WHERE n.t CONTAINS toLower(trim(coalesce($p.q, m.name))) RETURN count(*)",
        {"p": {"q": None}},
    )
    assert "MODEL DIMENSION BY (0 cs_i)" in beside, beside
    assert "JSON_VALUE(n1.props, '$.t' RETURNING CLOB) END AS v" in beside, beside
