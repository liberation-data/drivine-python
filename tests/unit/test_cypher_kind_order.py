"""
What an order sorts a value of open kind by, compiled: on Oracle where its kind comes, then the value, each carried out
of a `WITH` as a column of its own; a value the statement makes of one kind by its key alone; and Postgres as before
(liberation-data/drivine4j#307). Drivine for TypeScript's compile-only test of liberation-data/drivine4j#299
(liberation-data/drivine#371), ported for #175. The spec holds the rows since v0.6.16 (#299).
"""

from drivine.cypher.sql.oracle_cypher_compiler import OracleCypherCompiler
from drivine.cypher.sql.postgres_cypher_compiler import PostgresCypherCompiler

RANK = (
    "DECODE(JSON_VALUE(p2.s1, '$.type()'), "
    "'object', 1, 'array', 2, 'string', 3, 'boolean', 4, 'number', 5, 'double', 5, 'float', 5)"
)


def test_oracle_sorts_a_value_of_open_kind_by_where_its_kind_comes_then_by_the_value() -> None:
    sql = OracleCypherCompiler("g").compile("UNWIND [2, 'b'] AS t RETURN t ORDER BY t DESC").sql
    assert sql.endswith(f"ORDER BY {RANK} DESC, p2.s1 DESC"), sql
    # An item of a list that holds a null may be JSON's null, which ties with SQL's: the value is read as
    # `grouped_null` has it (#314).
    sql = OracleCypherCompiler("g").compile("UNWIND [2, 'b', null] AS t RETURN t ORDER BY t DESC").sql
    assert sql.endswith(f"ORDER BY {RANK} DESC, NVL(p2.s1, JSON('null')) DESC"), sql
    assert RANK in OracleCypherCompiler("g").compile("MATCH (n:Doc) RETURN n.a ORDER BY n.a").sql


def test_oracle_carries_each_term_out_of_a_with_as_a_column_of_its_own_and_sorts_by_both_after_it() -> None:
    sql = OracleCypherCompiler("g").compile("UNWIND [2, 'b'] AS t WITH t ORDER BY t SKIP 1 LIMIT 1 RETURN t").sql
    assert f"{RANK} AS s1_1, p2.s1 AS s1" in sql, sql
    assert "w3.s1_1 AS o1" in sql, sql
    assert "w3.s1 AS o2" in sql, sql
    assert sql.endswith("ORDER BY p4.o1, p4.o2"), sql


def test_oracle_sorts_a_value_the_statement_makes_of_one_kind_by_its_key_alone() -> None:
    for cypher in (
        "UNWIND [2, 'b'] AS t RETURN t ORDER BY toString(t)",
        "UNWIND [2, 'b'] AS t RETURN t ORDER BY t = 2",
        "UNWIND [2, 'b'] AS t RETURN t ORDER BY date('2024-01-01')",
        "UNWIND [2, 'b'] AS t RETURN t ORDER BY [t]",
        "UNWIND [2, 'b'] AS t WITH t ORDER BY toString(t) LIMIT 1 RETURN t",
        "MATCH (n:Doc) RETURN n.a ORDER BY n",
    ):
        sql = OracleCypherCompiler("g").compile(cypher).sql
        assert "'object', 1" not in sql, (cypher, sql)
        assert "s1_1" not in sql, (cypher, sql)


def test_postgres_sorts_a_value_of_open_kind_by_its_key_alone() -> None:
    sql = PostgresCypherCompiler("g").compile("UNWIND [2, 'b'] AS t WITH t ORDER BY t LIMIT 1 RETURN t").sql
    assert "s1_1" not in sql, sql
    assert "'object', 1" not in sql, sql
