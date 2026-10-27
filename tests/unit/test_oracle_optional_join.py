"""
On Oracle, an `OPTIONAL CALL` or an `OPTIONAL MATCH` is a lateral join on `1 = 1`: in a scalar subquery, as a
`COUNT { … }` is, Oracle refused a join on `TRUE` followed by `WHERE … AND …` (ORA-00907), and crashed the session on
one without (#200). See docs/oracle-on-true-in-scalar-subquery-crash.md. Postgres joins on `TRUE` still. drivine4j's
OracleOptionalJoinTest, ported.
"""

import re

from drivine.cypher.sql.oracle_cypher_compiler import OracleCypherCompiler
from drivine.cypher.sql.postgres_cypher_compiler import PostgresCypherCompiler

STATEMENTS = [
    "MATCH (n:CyOpt {id: 1}) RETURN COUNT { OPTIONAL CALL { MATCH (z:Nope) RETURN z.k AS u } "
    "MATCH (y:CyOpt) WHERE y.k = 'q' RETURN y }",
    "MATCH (n:CyOpt {id: 1}) RETURN COUNT { OPTIONAL CALL { MATCH (z:Nope) RETURN z.k AS u } RETURN u }",
    "MATCH (n:CyOpt {id: 1}) RETURN COUNT { OPTIONAL MATCH (z:Nope) RETURN z }",
    "OPTIONAL CALL { MATCH (z:Nope) RETURN z.k AS u } RETURN u",
    "UNWIND $ts AS t MATCH (n {id: t.id}) SET n.a = COUNT { OPTIONAL CALL { WITH t MATCH (z:Nope) RETURN t AS u } "
    "RETURN u }",
]


def test_an_optional_lateral_join_is_on_1_equals_1_not_on_true():
    for cypher in STATEMENTS:
        sql = OracleCypherCompiler("g").compile(cypher, {"ts": []}).sql
        assert re.search(r"LEFT JOIN LATERAL \(\n(.*\n)*?\s*\) [co]\d+ ON 1 = 1\n", sql), sql
        assert "ON TRUE" not in sql, sql


def test_a_call_that_is_not_optional_is_still_a_cross_join_and_postgres_still_joins_on_true():
    called = (
        OracleCypherCompiler("g")
        .compile("MATCH (n:CyOpt) RETURN COUNT { CALL { MATCH (z:Nope) RETURN z.k AS u } RETURN u }")
        .sql
    )
    assert re.search(r"CROSS JOIN LATERAL \(\n(.*\n)*?\s*\) c\d+\n", called), called
    assert " ON " not in called, called
    postgres = PostgresCypherCompiler("g").compile(STATEMENTS[1]).sql
    assert re.search(r"\) c\d+ ON TRUE", postgres), postgres
