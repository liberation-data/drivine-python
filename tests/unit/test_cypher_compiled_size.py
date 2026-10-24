"""
Operations nested in one another compile to a statement that grows with them, not exponentially: an operand read more
than once, to test its kind and then its value, is bound once where it is long. Unbound, the clause that stamps a node
compiled to 650 KB on Oracle. drivine4j's CompiledSizeTest, ported.
"""

import re

import pytest

from drivine.cypher.sql.oracle_cypher_compiler import OracleCypherCompiler
from drivine.cypher.sql.postgres_cypher_compiler import PostgresCypherCompiler
from drivine.cypher.sql.sql_cypher_compiler import SqlCypherCompiler
from drivine.model import stamps

COMPILERS = pytest.mark.parametrize(
    "compiler", [OracleCypherCompiler, PostgresCypherCompiler], ids=["oracle", "postgres"]
)


@COMPILERS
def test_the_clause_that_stamps_a_node_compiles_to_a_statement_of_a_few_kilobytes(
    compiler: type[SqlCypherCompiler],
) -> None:
    sql = compiler("g").compile(f"MATCH (c:Claim {{id: $id}}) SET {stamps.set_clause('c')}", {"id": "x"}).sql
    assert len(sql) < 20_000, len(sql)


@COMPILERS
def test_a_duration_added_to_a_value_read_from_the_store_grows_with_each_addition_not_exponentially(
    compiler: type[SqlCypherCompiler],
) -> None:
    def added(times: int) -> str:
        return "MATCH (c:Claim) RETURN " + "(" * times + "c.day" + " + duration('P1D'))" * times

    one, two, four = (len(compiler("g").compile(added(it)).sql) for it in (1, 2, 4))
    # drivine4j's bound is 20,000, which its 19,978 characters on Oracle just keep to: the JSON_TRANSFORM a result is
    # read through here (python-oracledb#622) is a few more.
    assert one < 21_000, one
    assert four < 3 * two, (two, four)


@COMPILERS
def test_a_with_sorted_by_a_duration_added_to_a_stored_value_none_of_its_items_grows_with_each_addition(
    compiler: type[SqlCypherCompiler],
) -> None:
    def added(times: int) -> str:
        term = "(" * times + "c.day" + " + duration('P1D'))" * times
        return f"MATCH (c:Claim) WITH c ORDER BY {term} RETURN c.id"

    # With no walk beside it, the term shares: unshared, as one beside a walk is (drivine4j#192), it grew exponentially.
    one, two, four = (len(compiler("g").compile(added(it)).sql) for it in (1, 2, 4))
    assert one < 40_000, one
    assert four < 3 * two, (two, four)


@pytest.mark.parametrize(
    "match",
    [
        "MATCH (:Person {name: 'Laurence'})-[*]->(b) WITH b",
        "MATCH (:Person {name: 'Laurence'})-[*]->(b) WITH b WITH b",
        "MATCH (:Person {name: 'Laurence'}) ((x)-[:KNOWS]->(y))+ (b) WITH b",
    ],
    ids=["walk", "walk-read-through-a-with", "quantified-path-pattern"],
)
def test_a_term_a_with_beside_a_walk_sorts_by_that_is_none_of_its_items_is_bound_in_no_subquery(match: str) -> None:
    # Bound in one, carried out of the WITH's query, it closed Oracle's session (drivine4j#192). With no walk, it is.
    def compiled(match: str) -> str:
        return OracleCypherCompiler("g").compile(f"{match} ORDER BY toString(coalesce(b.name, b.title)) RETURN b").sql

    assert re.search(r"\) sh\d+\)", compiled("MATCH (b:Person) WITH b"))
    assert not re.search(r"\) sh\d+\)", compiled(match))


@COMPILERS
def test_a_sum_of_twice_the_terms_compiles_to_about_twice_the_statement(compiler: type[SqlCypherCompiler]) -> None:
    def summed(terms: int) -> str:
        return "MATCH (c:Claim) RETURN " + " + ".join(f"c.p{it}" for it in range(1, terms + 1))

    short, long = (len(compiler("g").compile(summed(it)).sql) for it in (8, 16))
    assert long < 3 * short, (short, long)


@COMPILERS
def test_a_quantified_path_pattern_of_twice_the_hops_each_node_read_by_its_where_compiles_to_about_twice_the_statement(
    compiler: type[SqlCypherCompiler],
) -> None:
    def group(hops: int) -> str:
        chain = "".join(f"-[:R]->(n{it})" for it in range(1, hops + 1))
        where = " AND ".join(f"n{it - 1}.v < n{it}.v" for it in range(1, hops + 1))
        return f"MATCH (a:Claim) ((n0){chain} WHERE {where}){{1,3}} (b) RETURN b"

    short, long = (len(compiler("g").compile(group(it)).sql) for it in (4, 8))
    assert short < 20_000, short
    assert long < 3 * short, (short, long)
