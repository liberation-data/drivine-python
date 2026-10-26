"""
Operations nested in one another compile to a statement that grows with them, not exponentially: an operand read more
than once, to test its kind and then its value, is bound once where it is long. Unbound, the clause that stamps a node
compiled to 650 KB on Oracle. drivine4j's CompiledSizeTest, ported.
"""

import math
import re
from typing import Any

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
    # drivine4j's bound is 25,000, as a sum that may be a text writes each side as toString does, a float as Java
    # writes it; the JSON_TRANSFORM a result is read through here (python-oracledb#622) is a few characters more.
    assert one < 25_000, one
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


@COMPILERS
def test_items_taken_out_of_lists_nested_twice_as_deep_holding_a_relationship_compile_to_about_twice_the_statement(
    compiler: type[SqlCypherCompiler],
) -> None:
    def taken(depth: int) -> str:
        return f"MATCH (c:Claim)-[r:R]->() WITH {'[' * depth}r, 1{']' * depth} AS m RETURN m{'[0]' * depth} = 1"

    short, long = (len(compiler("g").compile(taken(it)).sql) for it in (4, 8))
    assert short < 20_000, short
    assert long < 3 * short, (short, long)


@pytest.mark.parametrize(
    ("compiler", "written"), [(PostgresCypherCompiler, "to_char("), (OracleCypherCompiler, "TO_CHAR(")]
)
def test_a_sum_writes_a_side_as_to_string_writes_a_float_only_where_the_statement_gives_neither_side(
    compiler: type[SqlCypherCompiler], written: str
) -> None:
    # A number the statement gives is written out at compile time, and a side added to one is a text where the sum is
    # one.
    def writes(cypher: str, parameters: dict[str, object] | None = None) -> int:
        return compiler("g").compile(cypher, parameters).sql.count(written)

    once = writes("MATCH (c:Claim) RETURN c.a + c.b") // 2
    assert once > 0
    assert writes("MATCH (c:Claim) RETURN c.a + 1, 2.5 + c.b, c.a + $n", {"n": 1.5}) == 0
    assert writes("MATCH (c:Claim) RETURN 'x' + c.b") == once
    # NaN and the infinities are bound as texts on Postgres, so both sides of a sum with one are written as toString
    # writes them.
    assert writes("MATCH (c:Claim) RETURN c.a + $n", {"n": float("nan")}) == 2 * once


@pytest.mark.parametrize(
    ("compiler", "written"), [(PostgresCypherCompiler, "to_char("), (OracleCypherCompiler, "TO_CHAR(")]
)
def test_a_sum_writes_a_side_the_statement_makes_a_text_as_its_own_text_and_one_that_may_be_a_float_as_to_string(
    compiler: type[SqlCypherCompiler], written: str
) -> None:
    # Written as toString writes a float, a side that is always a text made `count('x' + toString(i))` 2.5 times as slow
    # on Oracle (drivine4j#268).
    def writes(cypher: str) -> int:
        return compiler("g").compile(cypher).sql.count(written)

    # toString writes its float once, and the sum writes the text it gives as it is.
    once = writes("MATCH (c:Claim) RETURN toString(c.i)")
    assert once > 0
    assert writes("MATCH (c:Claim) RETURN 'x' + toString(c.i)") == once
    assert (
        writes(
            "MATCH (c:Claim) RETURN 'a' + toUpper(c.s), trim(c.s) + substring(c.s, 1) + left(c.s, 2) "
            "+ replace(c.s, 'a', 'b')"
        )
        == 0
    )
    assert writes("MATCH (c:Claim)-[r]->() RETURN split(c.s, ',')[0] + reverse(toLower(c.s)) + type(r)") == 0
    # Carried through a WITH, a collect and an UNWIND, still a text.
    assert (
        writes(
            "MATCH (c:Claim) WITH toLower(c.s) AS t, collect(toUpper(c.s)) AS ts UNWIND ts AS u "
            "RETURN t + u + ts[0] + head(ts)"
        )
        == 0
    )
    # A property may hold a float: it is written as toString writes it, beside a text or not.
    assert writes("MATCH (c:Claim) RETURN c.s + 'b'") == once
    assert writes("MATCH (c:Claim) RETURN toUpper(c.s) + c.f") == once
    assert writes("MATCH (c:Claim) RETURN 'x' + toString(c.i) + c.f") == 2 * once
    # A text put together with a number given, on either side, is a text.
    assert writes("MATCH (c:Claim) RETURN 1 + toUpper(c.s) + 'x', toUpper(c.s) + 2.5 + 'x'") == 0
    # A CASE, a coalesce and a list written are a text, or hold texts, where each of theirs is; a text unwound is one.
    assert (
        writes(
            "MATCH (c:Claim) RETURN CASE WHEN c.b THEN toUpper(c.s) ELSE 'n' END + coalesce(toLower(c.s), 'z') "
            "+ ['a', toUpper(c.s)][1]"
        )
        == 0
    )
    assert writes("MATCH (c:Claim) RETURN ltrim(c.s) + rtrim(c.s) + right(c.s, 2)") == 0
    assert writes("MATCH (c:Claim) UNWIND toUpper(c.s) AS u RETURN u + 'x'") == 0
    # A list of texts kept distinct, sliced, its tail, its last, and an item an expression names, still holds texts.
    assert (
        writes(
            "MATCH (c:Claim) WITH collect(DISTINCT toUpper(c.s)) AS ts "
            "RETURN last(ts) + ts[1..][0] + tail(ts)[0] + ts[size(ts) - 1]"
        )
        == 0
    )
    assert (
        writes(
            "MATCH (c:Claim) WITH collect(toUpper(c.s)) AS ts "
            "RETURN reverse(ts)[0] + coalesce(ts, split('a,b', ','))[0]"
        )
        == 0
    )
    # A CASE and a CALL's UNION hold texts where each of theirs does, and not where one may hold a float.
    case = (
        "MATCH (c:Claim) WITH collect(toUpper(c.s)) AS ts, collect(c.f) AS fs "
        "RETURN (CASE WHEN size(ts) > 1 THEN ts ELSE {} END)[0] + 'x'"
    )
    assert writes(case.format("['a']")) == 0
    assert writes(case.format("fs")) == once
    union = (
        "MATCH (c:Claim) CALL {{ WITH c RETURN split(c.s, ',') AS v UNION WITH c RETURN {} AS v }} RETURN v[0] + 'x'"
    )
    assert writes(union.format("['n']")) == 0
    assert writes(union.format("[c.f]")) == once
    # Joined to texts, too: the join writes its sides as any `+` does, and its item, added to a text, is written as it
    # is.
    joined = (
        "MATCH (c:Claim) WITH collect(toUpper(c.s)) AS ts, collect(c.f) AS fs "
        "RETURN (ts + ['a'])[0], (ts + 'a')[0], ('b' + ts)[0], (ts + fs)[0]"
    )
    assert writes(joined.replace("[0],", "[0] + 'x',")) == writes(joined)
    assert writes(f"{joined} + 'x'") == writes(joined) + once
    # Each query of a CALL's UNION makes it a text, and a write carries it on.
    assert (
        writes("MATCH (c:Claim) CALL { WITH c RETURN toUpper(c.s) AS v UNION WITH c RETURN 'n' AS v } RETURN v + 'x'")
        == 0
    )
    assert writes("MATCH (c:Claim) WITH c, toUpper(c.s) AS t SET c.t = t RETURN t + 'x'") == 0
    # Where any one of them may be a float, it is written as toString writes it.
    assert writes("MATCH (c:Claim) RETURN CASE WHEN c.b THEN 'a' ELSE c.f END + 'x'") == once
    assert writes("MATCH (c:Claim) RETURN coalesce(toUpper(c.s), c.f) + 'x'") == once
    assert writes("MATCH (c:Claim) RETURN ['a', c.f][1] + 'x'") == once
    assert (
        writes("MATCH (c:Claim) CALL { WITH c RETURN toUpper(c.s) AS v UNION WITH c RETURN c.f AS v } RETURN v + 'x'")
        == once
    )
    # The least and the greatest of texts are a text; of a property, which may hold a float, not.
    assert writes("MATCH (c:Claim) WITH min(toUpper(c.s)) AS lo, max(toLower(c.s)) AS hi RETURN lo + hi + 'x'") == 0
    assert writes("MATCH (c:Claim) WITH max(c.f) AS hi RETURN hi + 'x'") == once


def test_a_sort_by_a_property_on_postgres_is_by_its_json_alone_and_only_a_list_the_statement_makes_is_keyed_as_a_list():
    # Keyed as a list, a term costs thousands a row in the planner's estimate, which brings in its JIT for the whole
    # statement past a few dozen rows (drivine4j#223).
    def ordered(cypher: str) -> str:
        return PostgresCypherCompiler("g").compile(cypher).sql.split("ORDER BY ", 1)[1]

    assert ordered("MATCH (c:Claim) RETURN c.name ORDER BY c.name") == "NULLIF(p2.s1, 'null'::jsonb)"
    listed = ordered("MATCH (c:Claim) WITH c.name AS n, collect(c.id) AS ids RETURN n ORDER BY ids")
    assert "WITH RECURSIVE" in listed, listed
    # Its items as arrays unnested, which the planner takes for 10 items, not a set-returning function's 100: a 150th
    # of the cost.
    assert "FROM jsonb_array_elements(" not in listed and "unnest(ARRAY(SELECT jsonb_array_elements(" in listed, listed


@COMPILERS
def test_an_equality_of_values_that_hold_no_null_in_a_list_or_a_map_compiles_with_no_test_for_one(
    compiler: type[SqlCypherCompiler],
) -> None:
    # #116: a null a list or a map holds is tested for, and the two walked, only where one may hold one.
    test = {OracleCypherCompiler: "[[,:]null[],}]", PostgresCypherCompiler: "jsonb_path_exists("}[compiler]

    def tests(cypher: str, parameters: dict[str, Any] | None = None) -> bool:
        return test in compiler("g").compile(cypher, parameters).sql

    assert not tests("MATCH (c:Claim) WHERE c.list = $p RETURN c.id", {"p": [1, 2]})
    assert not tests(
        "MATCH (c:Claim) WITH c, c.list AS l RETURN [1, 2] = [1, 2], c.a = c.b, l = c.b, c.list IN $ls, "
        "{x: 1} <> {x: 2}, size(l) = 2",
        {"ls": [[1]]},
    )
    # Lists and maps written out are compared item by item.
    assert not tests("MATCH (c:Claim) WITH c.a AS a, c.b AS b RETURN [a, b] = [b, a], {x: a} = {x: b}, a IN [b, null]")
    # A parameter's list that holds a null is compared item by item as the statement is compiled; a map is walked.
    assert not tests("MATCH (c:Claim) WHERE c.list = $p RETURN c.id", {"p": [1, None, [None, 2]]})
    assert tests("MATCH (c:Claim) WHERE c.map = $p RETURN c.id", {"p": {"x": None}})
    assert tests("MATCH (c:Claim) WITH c, [c.a] AS l RETURN l = [c.b]")
    # As is a parameter's list with an item too long to write in the statement.
    assert tests("MATCH (c:Claim) WHERE c.list = $p RETURN c.id", {"p": ["x" * 2000, None]})
    # Up to LITERAL_BYTES of JSON's text, its quotes and each character's UTF-8 bytes counted, an item is written.
    for text, walked in (("x" * 998, False), ("x" * 999, True), ("é" * 499, False), ("é" * 500, True)):
        assert tests("MATCH (c:Claim) WHERE c.list = $p RETURN c.id", {"p": [[text], None]}) is walked, len(text)
    # Read from what the parameter is bound as: an iterator is read once, and NaN is a value, bound as a text.
    assert not tests("MATCH (c:Claim) WHERE c.list = $p RETURN c.id", {"p": iter([1, 2])})
    assert not tests("MATCH (c:Claim) WHERE c.list = $p RETURN c.id", {"p": [math.nan]})


@COMPILERS
def test_a_value_that_holds_no_null_carried_past_a_write_compiles_with_no_test_for_one(
    compiler: type[SqlCypherCompiler],
) -> None:
    # A row carried between a statement's writes holds each value as its JSON, which holds no null where the value held
    # none (drivine#340).
    test = {OracleCypherCompiler: "[[,:]null[],}]", PostgresCypherCompiler: "jsonb_path_exists("}[compiler]
    compiled = compiler("g").compile(
        "MATCH (c:Claim) WITH c, c.list AS l SET c.b = 1 WITH c, l WHERE l = c.other RETURN c.id"
    )
    assert not any(test in it for it in (compiled.sql, compiled.prelude or "")), compiled.sql


@COMPILERS
def test_a_where_that_searches_a_list_that_holds_no_null_compiles_to_the_search_alone(
    compiler: type[SqlCypherCompiler],
) -> None:
    # #116: null and false keep no row alike, so a WHERE needs no test of the value being null.
    def compiled(cypher: str, parameters: dict[str, Any] | None = None) -> str:
        return compiler("g").compile(cypher, parameters).sql

    def tested(sql: str) -> bool:
        return "ELSE FALSE" in sql or "THEN FALSE" in sql

    sql = compiled("MATCH (c:Claim) WHERE c.a IN $l AND (c.b IN [1, 2] OR c.c > 1) RETURN c.id", {"l": [1, 2]})
    assert not tested(sql), sql
    # Nor of the list being null or holding a null, beside a value that holds none.
    nullable = compiled("MATCH (c:Claim) WHERE 'a' IN c.tags AND c.a IN $l RETURN c.id", {"l": [1, None]})
    assert not tested(nullable), nullable
    # Nor in a quantified path pattern's WHERE, which keeps a step where it is true.
    stepped = compiled("MATCH (a:Claim) ((x)-[:R]->(y) WHERE y.a IN [1, 2]){1,3} (b) RETURN b.id")
    assert not tested(stepped), stepped
    returned = compiled("MATCH (c:Claim) RETURN c.a IN $l", {"l": [1, 2]})
    assert "THEN FALSE" in returned, returned


@COMPILERS
def test_an_in_of_an_in_compiles_to_a_statement_that_grows_with_each_not_exponentially(
    compiler: type[SqlCypherCompiler],
) -> None:
    def nested(times: int, list_: str) -> str:
        return "MATCH (c:Claim) RETURN " + "(" * times + "c.a" + f" IN {list_})" * times

    for list_ in ("[true]", "c.list"):
        two, four = (len(compiler("g").compile(nested(it, list_)).sql) for it in (2, 4))
        assert four < 3 * two, (list_, two, four)


def test_values_that_may_hold_a_null_are_walked_on_postgres_over_arrays_unnested_which_the_planner_costs_low():
    # As the sort key's walk is: a set-returning function's 100 rows, multiplied through the walk, bring in its JIT.
    sql = PostgresCypherCompiler("g").compile("MATCH (c:Claim) WITH [c.a] AS l, [c.b] AS m RETURN l = m").sql
    assert "WITH RECURSIVE" in sql and "FROM jsonb_array_elements(" not in sql and "jsonb_each(" not in sql, sql


@COMPILERS
@pytest.mark.parametrize("equality", ["[a, b] = [b, a]", "{x: a} = {x: b}"])
def test_lists_and_maps_written_out_that_may_hold_a_null_are_compared_item_by_item_not_built_and_compared_whole(
    compiler: type[SqlCypherCompiler], equality: str
) -> None:
    built = {OracleCypherCompiler: ("JSON_ARRAY(", "JSON_OBJECT("), PostgresCypherCompiler: ("json_build_",)}[compiler]
    sql = compiler("g").compile(f"MATCH (c:Claim) WITH c.a AS a, c.b AS b RETURN {equality} AS e").sql
    assert not any(it in sql for it in built), sql


def test_oracle_declares_the_function_that_compares_values_that_may_hold_a_null_once_and_none_in_a_procedure():
    # A procedure's queries call no function it declares: there, two values equal as JSON are null, as each holds one.
    def compiled(cypher: str) -> str:
        return OracleCypherCompiler("g").compile(cypher).sql

    read = compiled("WITH [1, null] AS a, [2, null] AS b RETURN [a = b, b = a]")
    assert read.count("FUNCTION cy_nulls_equal(") == 1 and "too_long" in read, read
    written = compiled("CREATE (n:X) WITH n, [1, null] AS a, [2, null] AS b SET n.e = a = b")
    assert "cy_nulls_equal" not in written and "THEN NULL ELSE FALSE END" in written, written


@COMPILERS
def test_a_round_whose_places_the_statement_gives_writes_only_the_rounding_of_those_places(
    compiler: type[SqlCypherCompiler],
) -> None:
    # Both roundings, and a CASE to pick one when the row is read, made a nested round of an aggregate, which shares
    # nothing, grow by both at each level: on Postgres, four deep, ten times as long.
    def nested(rounded: str) -> str:
        term = "sum(x)"
        for _ in range(4):
            term = rounded.format(term)
        return compiler("g").compile(f"UNWIND [1.5, -2.5] AS x RETURN {term}").sql

    assert nested("round({}, 0)") == nested("round({})")
    assert nested("round({}, -0.5)") == nested("round({})")
    assert "FLOOR(" not in nested("round({}, 1)")
    assert "FLOOR(" in nested("round({}, toInteger('0'))")


@COMPILERS
@pytest.mark.parametrize("mode", ["", ", 'HALF_UP'"], ids=["two-arguments", "a-mode"])
def test_a_round_whose_places_come_from_data_reads_the_value_once_more_for_its_kind_not_in_the_places(
    compiler: type[SqlCypherCompiler], mode: str
) -> None:
    # A null value rounds to null by negative places, so the value is read where they are refused. Read in the places,
    # it was written out again at each place a store writes them, under an aggregate, which shares nothing: three
    # rounds deep made up to 26 MB on Oracle and 550 KB on Postgres, now 1.3 MB and 48 KB.
    nested = "UNWIND [1.5] AS x WITH x, 1 AS p RETURN p, " + "round(" * 3 + "sum(x)" + f", p{mode})" * 3
    sql = compiler("g").compile(nested).sql
    assert len(sql) < (2_000_000 if compiler is OracleCypherCompiler else 100_000), len(sql)
