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
def test_two_list_literals_of_a_hundred_items_a_null_among_them_compared_item_by_item_are_a_few_hundred_characters_each(
    compiler: type[SqlCypherCompiler],
) -> None:
    # Each pair of literal items tested on both sides for a text past 4,000 bytes made 80,907 characters on Oracle in
    # drivine4j, about four times the 20,879 before (liberation-data/drivine4j#476).
    def item(i: int) -> str:
        return [f"{i}", f"'s{i}'", "null", f"{i}.5", "true", f"[{i}, 'n']", f"{{k: {i}}}"][i % 7]

    items = "[" + ", ".join(item(i) for i in range(100)) + "]"
    sql = compiler("g").compile(f"RETURN {items} = {items}").sql
    assert len(sql) < 25_000, len(sql)


@COMPILERS
def test_a_duration_added_to_a_value_read_from_the_store_grows_with_each_addition_not_exponentially(
    compiler: type[SqlCypherCompiler],
) -> None:
    def added(times: int) -> str:
        return "MATCH (c:Claim) RETURN " + "(" * times + "c.day" + " + duration('P1D'))" * times

    one, two, four = (len(compiler("g").compile(added(it)).sql) for it in (1, 2, 4))
    # drivine4j's bound is 30,000, as a sum that may be a text writes each side as toString does, a float as Java
    # writes it: on Oracle twice, the second time for a text past 4,000 bytes (#168). The JSON_TRANSFORM a result is
    # read through here (python-oracledb#622) is a few characters more.
    assert one < 30_000, one
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
    # A property may hold a float: it is written as toString writes it, beside a text or not. Oracle's sum writes such a
    # side twice, the second time in the branch it takes for a text past 4,000 bytes (#168).
    side = 2 * once if compiler is OracleCypherCompiler else once
    assert writes("MATCH (c:Claim) RETURN c.s + 'b'") == side
    assert writes("MATCH (c:Claim) RETURN toUpper(c.s) + c.f") == side
    assert writes("MATCH (c:Claim) RETURN 'x' + toString(c.i) + c.f") == once + side
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
    assert writes(case.format("fs")) == side
    union = (
        "MATCH (c:Claim) CALL {{ WITH c RETURN split(c.s, ',') AS v UNION WITH c RETURN {} AS v }} RETURN v[0] + 'x'"
    )
    assert writes(union.format("['n']")) == 0
    assert writes(union.format("[c.f]")) == side
    # Joined to texts, too: the join writes its sides as any `+` does, and its item, added to a text, is written as it
    # is.
    joined = (
        "MATCH (c:Claim) WITH collect(toUpper(c.s)) AS ts, collect(c.f) AS fs "
        "RETURN (ts + ['a'])[0], (ts + 'a')[0], ('b' + ts)[0], (ts + fs)[0]"
    )
    assert writes(joined.replace("[0],", "[0] + 'x',")) == writes(joined)
    assert writes(f"{joined} + 'x'") == writes(joined) + side
    # Each query of a CALL's UNION makes it a text, and a write carries it on.
    assert (
        writes("MATCH (c:Claim) CALL { WITH c RETURN toUpper(c.s) AS v UNION WITH c RETURN 'n' AS v } RETURN v + 'x'")
        == 0
    )
    assert writes("MATCH (c:Claim) WITH c, toUpper(c.s) AS t SET c.t = t RETURN t + 'x'") == 0
    # Where any one of them may be a float, it is written as toString writes it.
    assert writes("MATCH (c:Claim) RETURN CASE WHEN c.b THEN 'a' ELSE c.f END + 'x'") == side
    assert writes("MATCH (c:Claim) RETURN coalesce(toUpper(c.s), c.f) + 'x'") == side
    assert writes("MATCH (c:Claim) RETURN ['a', c.f][1] + 'x'") == side
    assert (
        writes("MATCH (c:Claim) CALL { WITH c RETURN toUpper(c.s) AS v UNION WITH c RETURN c.f AS v } RETURN v + 'x'")
        == side
    )
    # The least and the greatest of texts are a text; of a property, which may hold a float, not.
    assert writes("MATCH (c:Claim) WITH min(toUpper(c.s)) AS lo, max(toLower(c.s)) AS hi RETURN lo + hi + 'x'") == 0
    assert writes("MATCH (c:Claim) WITH max(c.f) AS hi RETURN hi + 'x'") == side


def test_a_sort_by_a_property_on_postgres_is_by_json_and_text_and_only_a_list_the_statement_makes_is_keyed_as_a_list():
    # Keyed as a list, a term costs thousands a row in the planner's estimate, which brings in its JIT for the whole
    # statement past a few dozen rows (drivine4j#223). A text sorts by its code points (#161), in the same row, with no
    # subquery.
    def ordered(cypher: str) -> str:
        return PostgresCypherCompiler("g").compile(cypher).sql.split("ORDER BY ", 1)[1]

    assert ordered("MATCH (c:Claim) RETURN c.name ORDER BY c.name") == (
        "ROW(CASE WHEN jsonb_typeof(p2.s1) = 'string' THEN '\"\"'::jsonb ELSE NULLIF(p2.s1, 'null'::jsonb) END, "
        "(CASE WHEN jsonb_typeof(p2.s1) = 'string' THEN (p2.s1 #>> '{}') END) COLLATE \"C\")"
    )
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
    # Nor of the list being null or holding a null, beside a value that holds none. On Oracle an item is compared with
    # 'a' as JSON where it is read as a VARCHAR2 or is no text, nor a list or a map; else it is not equal (#291,
    # drivine4j#476).
    nullable = compiled("MATCH (c:Claim) WHERE 'a' IN c.tags AND c.a IN $l RETURN c.id", {"l": [1, None]})
    assert not tested(nullable.replace(" = JSON('\"a\"') ELSE FALSE END", "")), nullable
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


def test_values_that_may_hold_a_null_are_walked_on_oracle_by_the_stores_function_where_they_differ_as_json():
    # drivine4j's CompiledSizeTest of #270 (#188): a procedure's queries, and a reduce's step's, call no function of
    # their own, so the store keeps it.
    write = "MATCH (c:Claim) WITH c, [c.a, null] AS l, [c.b] AS m SET c.e = l = m"
    stored = OracleCypherCompiler("g").compile(write)
    assert stored.calls_stored_nulls_equal, stored.sql
    assert "THEN NULL ELSE (g_nulls_equal(" in stored.sql and "cy_nulls_equal" not in stored.sql, stored.sql
    # Two written as the same JSON text are null before any walk.
    text = r"JSON_SERIALIZE\((.+?) RETURNING VARCHAR2\(4000\) NULL ON ERROR\)"
    assert re.search(rf"CASE WHEN {text} = {text} THEN NULL ELSE \(g_nulls_equal\(\1, \2\) = 1\) END", stored.sql, re.S)
    reduced = OracleCypherCompiler("g").compile(
        "MATCH (c:Claim) WITH [c.a, null] AS l RETURN reduce(acc = true, x IN [[1]] | acc AND x = l)"
    )
    assert reduced.calls_stored_nulls_equal, reduced.sql
    assert "THEN NULL ELSE (g_nulls_equal(" in reduced.sql and "cy_nulls_equal" not in reduced.sql, reduced.sql
    # A read calls the store's too, and declares no function of its own for it.
    read = OracleCypherCompiler("g").compile("MATCH (c:Claim) WITH [c.a, null] AS l, [c.b] AS m RETURN l = m")
    assert read.calls_stored_nulls_equal and not read.sql.startswith("WITH\nFUNCTION"), read.sql
    assert "THEN NULL ELSE (g_nulls_equal(" in read.sql, read.sql
    # Without it, two that differ as JSON are false in a procedure, and a read declares the function itself, once.
    fallback = OracleCypherCompiler("g", nulls_equal_stored=False).compile(write)
    assert not fallback.calls_stored_nulls_equal, fallback.sql
    assert "nulls_equal" not in fallback.sql and "THEN NULL ELSE FALSE END" in fallback.sql, fallback.sql
    # And so are they in a reduce's step, which calls no function the query declares.
    unfolded = OracleCypherCompiler("g", nulls_equal_stored=False).compile(
        "MATCH (c:Claim) WITH [c.a, null] AS l RETURN reduce(acc = true, x IN [[1]] | acc AND x = l)"
    )
    assert not unfolded.calls_stored_nulls_equal, unfolded.sql
    assert "nulls_equal" not in unfolded.sql and "THEN NULL ELSE FALSE END" in unfolded.sql, unfolded.sql
    declared = OracleCypherCompiler("g", nulls_equal_stored=False).compile(
        "MATCH (c:Claim) WITH [c.a, null] AS l, [c.b] AS m RETURN [l = m, m = l]"
    )
    assert not declared.calls_stored_nulls_equal, declared.sql
    assert declared.sql.startswith("WITH\nFUNCTION cy_nulls_equal(") and "too_long" in declared.sql, declared.sql
    assert declared.sql.count("FUNCTION cy_nulls_equal(") == 1, declared.sql
    assert "THEN NULL ELSE (cy_nulls_equal(" in declared.sql, declared.sql
    # A statement that compares no value that may hold a null calls no function.
    flat = OracleCypherCompiler("g").compile("MATCH (c:Claim) SET c.e = c.a = c.b, c.f = c.list = [1]")
    assert not flat.calls_stored_nulls_equal, flat.sql
    # And calling the store's function makes a statement smaller than declaring it.
    alone = OracleCypherCompiler("g", nulls_equal_stored=False).compile(
        "MATCH (c:Claim) WITH [c.a, null] AS l, [c.b] AS m RETURN l = m"
    )
    assert len(read.sql) < len(alone.sql), (len(read.sql), len(alone.sql))


@pytest.mark.parametrize(
    "statement",
    [
        "MATCH (n) RETURN count(CASE WHEN n.l < [2] THEN 1 END)",
        "MATCH (n) RETURN count(CASE WHEN n.l < [2] AND (datetime(n.at) + duration({minutes: n.m})).minute > 0 "
        "THEN 1 END)",
        "MATCH (a)-[:NEXT*1..2]->(b) RETURN a.m AS a, count(*) AS c "
        "ORDER BY sum(CASE WHEN b.l < [2] THEN 1 ELSE 0 END)",
        "RETURN [[1] < [2], [1] <= [2], [3] > [2]]",
    ],
)
def test_two_lists_compared_declare_oracles_function_once_however_often_it_is_called(statement: str) -> None:
    # drivine4j's CompiledSizeTest of #349: in an aggregate's argument that binds a value or not, and in an order term
    # beside a walk.
    PostgresCypherCompiler("g").compile(statement)
    sql = OracleCypherCompiler("g").compile(statement).sql
    assert len(re.findall(r"FUNCTION cy_list_order\(", sql)) == 1, sql
    assert len(re.findall(r"\bcy_list_order\(", sql)) > 1, sql


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


@COMPILERS
def test_a_sign_nested_under_an_aggregate_compiles_to_about_the_statement_a_ceil_does_reading_its_number_once(
    compiler: type[SqlCypherCompiler],
) -> None:
    # Under an aggregate nothing is bound, so a sign that read its number twice wrote it out twice at each level: on
    # Oracle, where a sign is not SIGN alone (#163), four deep was 14 times the statement a ceil() is.
    def nested(function: str) -> int:
        return len(compiler("g").compile("UNWIND [1.5] AS x RETURN " + f"{function}(" * 4 + "sum(x)" + ")" * 4).sql)

    sign, ceil = nested("sign"), nested("ceil")
    assert sign < ceil * 1.1, f"sign {sign}, ceil {ceil} characters"


@COMPILERS
@pytest.mark.parametrize("kind", ["any", "all", "none", "single"])
def test_quantifiers_nested_twice_as_deep_over_lists_that_may_be_null_compile_to_about_twice_the_statement(
    compiler: type[SqlCypherCompiler], kind: str
) -> None:
    # Three-valued (#159), each one reads its list twice, to test it for null and then for its items: bound once where
    # it is long, as drivine4j binds it, so it is not written out again at each depth.
    def nested(depth: int) -> str:
        condition = f"x{depth} = 1"
        for it in range(depth, 0, -1):
            condition = f"{kind}(x{it} IN x{it - 1} WHERE {condition})"
        return f"MATCH (c:Claim) WITH c.lists AS x0 RETURN {condition}"

    short, long = (len(compiler("g").compile(nested(it)).sql) for it in (4, 8))
    assert short < 20_000, short
    assert long < 3 * short, (short, long)


@COMPILERS
def test_a_sum_reads_each_value_it_sums_as_often_as_an_avg_does_and_grows_under_functions_as_an_avg_does(
    compiler: type[SqlCypherCompiler],
) -> None:
    # #189: on Oracle a sum tests whether any value it sums is a float. Nothing is bound under an aggregate, so a test
    # that read each value as number() does, three times more, made a sum over nested functions twice the statement.
    def over(aggregate: str) -> str:
        return f"MATCH (c:Claim) RETURN {aggregate}(" + "ceil(" * 3 + "c.p" + ")" * 3 + ")"

    def under(aggregate: str, depth: int) -> str:
        return "UNWIND [1.5] AS x RETURN " + "sign(" * depth + f"{aggregate}(x)" + ")" * depth

    def size(cypher: str) -> int:
        return len(compiler("g").compile(cypher).sql)

    sum_, avg = (size(over(it)) for it in ("sum", "avg"))
    assert sum_ < avg * 1.1, f"{over('sum')}: sum {sum_}, avg {avg} characters"
    sum_growth, avg_growth = (size(under(it, 4)) / size(under(it, 2)) for it in ("sum", "avg"))
    assert sum_growth < avg_growth * 1.1, f"from 2 levels to 4, sum grew {sum_growth} times, avg {avg_growth}"


Z = "datetime('2024-03-15T10:00:00+02:00')"
WITHIN = "datetime('2024-03-15T09:30:00Z')"
AGGREGATING = [
    *(
        (
            f"UNWIND range(1, 3) AS i RETURN {aggregate}({argument})",
            f"UNWIND range(1, 3) AS i WITH {argument} AS t RETURN {aggregate}(t)",
        )
        for aggregate in ("count", "collect", "min", "max", "sum", "avg")
        for argument in [
            f"({Z} + duration({{minutes: i}})).minute"
            if aggregate in ("sum", "avg")
            else f"{Z} + duration({{minutes: i}})"
        ]
    ),
    (
        f"UNWIND range(1, 3) AS i WITH i % 2 AS k, collect({Z} + duration({{minutes: i}})) AS l RETURN k ORDER BY l",
        f"UNWIND range(1, 3) AS i WITH i % 2 AS k, {Z} + duration({{minutes: i}}) AS t "
        "WITH k, collect(t) AS l RETURN k ORDER BY l",
    ),
    (
        f"UNWIND range(1, 3) AS i RETURN count(DISTINCT {Z} + duration({{minutes: i}}))",
        f"UNWIND range(1, 3) AS i WITH {Z} + duration({{minutes: i}}) AS t RETURN count(DISTINCT t)",
    ),
    (
        f"MATCH (n:E) RETURN sum(size([x IN [datetime(n.at), datetime(n.at) + duration('PT1H')] WHERE x > {WITHIN}]))",
        f"MATCH (n:E) WITH size([x IN [datetime(n.at), datetime(n.at) + duration('PT1H')] WHERE x > {WITHIN}]) AS s "
        "RETURN sum(s)",
    ),
    # Sorted beside a variable-length walk by an aggregate that is none of the items.
    (
        "MATCH (a:E)-[:NEXT*1..2]->(b:E) RETURN a.id AS a, count(*) AS c "
        "ORDER BY sum((datetime(b.at) + duration({minutes: b.m})).minute)",
        "MATCH (a:E)-[:NEXT*1..2]->(b:E) WITH a, (datetime(b.at) + duration({minutes: b.m})).minute AS t "
        "RETURN a.id AS a, count(*) AS c ORDER BY sum(t)",
    ),
    # A text function or + in an aggregate: liberation-data/drivine-python#193.
    (
        "UNWIND range(1, 500) AS i WITH collect(DISTINCT 'value number ' + toString(i % 400)) AS xs RETURN size(xs)",
        "UNWIND range(1, 500) AS i WITH 'value number ' + toString(i % 400) AS t "
        "WITH collect(DISTINCT t) AS xs RETURN size(xs)",
    ),
    (
        "MATCH (n) RETURN collect(n.title + ': ' + n.first + ' ' + n.last)",
        "MATCH (n) WITH n.title + ': ' + n.first + ' ' + n.last AS t RETURN collect(t)",
    ),
]
"""Each aggregating statement, and the same with a WITH working the aggregate's argument out first."""


@COMPILERS
@pytest.mark.parametrize(("aggregating", "first"), AGGREGATING)
def test_an_aggregate_of_temporal_arithmetic_compiles_to_about_the_statement_working_its_argument_out_first_does(
    compiler: type[SqlCypherCompiler], aggregating: str, first: str
) -> None:
    # liberation-data/drivine4j#333: an aggregate's argument was compiled with nothing bound, so
    # count(Z + duration({minutes: i})) wrote the duration's fields and the addition's operands out again at each level.
    # Worked out first, for each row, it binds them as any item does.
    size, reference = (len(compiler("g").compile(it).sql) for it in (aggregating, first))
    assert size < 120_000 and size < reference * 1.2, f"{aggregating}: {size} characters, {reference} with a WITH first"


def _four(function: str, over: str) -> str:
    return f"{function}(" * 4 + over + ")" * 4


_TEXTS = "max(n.title) + ': ' + max(n.first) + ' ' + max(n.last)"
_WORKED = "WITH max(n.title) AS a, max(n.first) AS b, max(n.last) AS c"
_DAYS = "((((n.day + duration('P1D')) + duration('P1D')) + duration('P1D')) + duration('P1D'))"

OVER_AGGREGATES = [
    (f"MATCH (n) RETURN {_TEXTS}", f"MATCH (n) {_WORKED} RETURN a + ': ' + b + ' ' + c"),
    (
        "MATCH (n) RETURN head(collect(n.title)) + ': ' + head(collect(n.first)) + ' ' + head(collect(n.last))",
        "MATCH (n) WITH collect(n.title) AS a, collect(n.first) AS b, collect(n.last) AS c "
        "RETURN head(a) + ': ' + head(b) + ' ' + head(c)",
    ),
    (
        f"MATCH (n) RETURN {_four('toLower', 'max(n.name)')}",
        f"MATCH (n) WITH max(n.name) AS m RETURN {_four('toLower', 'm')}",
    ),
    (
        f"MATCH (n) RETURN {_four('toLower', 'head(collect(n.name))')}",
        f"MATCH (n) WITH collect(n.name) AS c RETURN {_four('toLower', 'head(c)')}",
    ),
    (
        f"MATCH (n) RETURN {_four('toLower', 'collect(n.name)[0]')}",
        f"MATCH (n) WITH collect(n.name) AS c RETURN {_four('toLower', 'c[0]')}",
    ),
    (
        f"MATCH (n) RETURN {_four('trim', 'max(n.name)')}",
        f"MATCH (n) WITH max(n.name) AS m RETURN {_four('trim', 'm')}",
    ),
    (
        f"MATCH (n) RETURN {_four('toString', 'head(collect(n.name))')}",
        f"MATCH (n) WITH collect(n.name) AS c RETURN {_four('toString', 'head(c)')}",
    ),
    (
        "MATCH (n) RETURN substring(substring(substring(substring(max(n.name), 0), 0), 0), 0)",
        "MATCH (n) WITH max(n.name) AS m RETURN substring(substring(substring(substring(m, 0), 0), 0), 0)",
    ),
    (
        "MATCH (n) RETURN (sum(n.a) + sum(n.b)) * 2 / count(*) - max(n.a)",
        "MATCH (n) WITH sum(n.a) AS a, sum(n.b) AS b, count(*) AS c, max(n.a) AS m RETURN (a + b) * 2 / c - m",
    ),
    # Beside a grouping key, with DISTINCT, as an order term, and beside a variable-length walk.
    (
        f"MATCH (n) RETURN n.k AS k, {_TEXTS} AS t",
        "MATCH (n) WITH n.k AS k, max(n.title) AS a, max(n.first) AS b, max(n.last) AS c "
        "RETURN k, a + ': ' + b + ' ' + c AS t",
    ),
    (f"MATCH (n) RETURN DISTINCT {_TEXTS}", f"MATCH (n) {_WORKED} RETURN DISTINCT a + ': ' + b + ' ' + c"),
    (
        f"MATCH (n) RETURN n.k AS k ORDER BY {_TEXTS}",
        "MATCH (n) WITH n.k AS k, max(n.title) AS a, max(n.first) AS b, max(n.last) AS c "
        "RETURN k ORDER BY a + ': ' + b + ' ' + c",
    ),
    # An order term that reads an item by its name, which ran drivine4j's compiler out of memory.
    (
        f"MATCH (n) RETURN n.k AS k, count(*) AS c ORDER BY {_four('toString', 'count(*) * 10 + size(k)')}",
        f"MATCH (n) WITH n.k AS k, count(*) AS x RETURN k, x AS c ORDER BY {_four('toString', 'x * 10 + size(k)')}",
    ),
    # And one where a comprehension's own name hides an item's, or the item's expression reads a name the
    # comprehension binds.
    (
        f"MATCH (n) RETURN n.k AS k, sum(n.a) AS m ORDER BY {_four('toString', '[k IN [1] | k + m][0]')} "
        "+ toString(sum(n.a))",
        "MATCH (n) WITH n.k AS k, sum(n.a) AS x RETURN k, x AS m "
        f"ORDER BY {_four('toString', '[k IN [1] | k + x][0]')} + toString(x)",
    ),
    (
        f"MATCH (n) RETURN n.k AS k, sum(n.a) AS m ORDER BY {_four('toString', '[n IN [1] | n + size(k)][0]')} "
        "+ toString(sum(n.a))",
        "MATCH (n) WITH n.k AS k, sum(n.a) AS x RETURN k, x AS m "
        f"ORDER BY {_four('toString', '[n IN [1] | n + size(k)][0]')} + toString(x)",
    ),
    # And one that reads an item nested deep, which drivine4j's trial compile once compiled with nothing bound.
    (
        f"MATCH (n) RETURN {_DAYS} AS k, count(*) AS c ORDER BY {_four('toString', 'count(*)')} + toString(k)",
        f"MATCH (n) WITH {_DAYS} AS k, count(*) AS x RETURN k, x AS c ORDER BY {_four('toString', 'x')} + toString(k)",
    ),
    (
        "MATCH (n)-[*1..3]->(m) RETURN max(m.title) + ': ' + max(m.first) + ' ' + max(m.last)",
        "MATCH (n)-[*1..3]->(m) WITH max(m.title) AS a, max(m.first) AS b, max(m.last) AS c "
        "RETURN a + ': ' + b + ' ' + c",
    ),
    (
        f"MATCH (n)-[*1..3]->(m) RETURN {_four('toLower', 'max(m.name)')}",
        f"MATCH (n)-[*1..3]->(m) WITH max(m.name) AS x RETURN {_four('toLower', 'x')}",
    ),
    (
        f"MATCH (n)-[*1..3]->(m) RETURN n.k AS k ORDER BY {_four('toLower', 'max(m.name)')}",
        f"MATCH (n)-[*1..3]->(m) WITH n.k AS k, max(m.name) AS x RETURN k ORDER BY {_four('toLower', 'x')}",
    ),
    # A text looked for in another, which Oracle's CONTAINS binds in a subquery past INSTR's 32,767 units
    # (drivine4j#312): over an aggregate's result, only over its column.
    (
        "MATCH (n) RETURN max(n.title) CONTAINS min(n.first)",
        "MATCH (n) WITH max(n.title) AS a, min(n.first) AS b RETURN a CONTAINS b",
    ),
]
"""Each statement over an aggregate's result, and the same with a WITH working its aggregates out first."""


@COMPILERS
@pytest.mark.parametrize(("over", "first"), OVER_AGGREGATES)
def test_text_functions_texts_added_and_arithmetic_over_an_aggregates_result_compile_to_about_a_with_first(
    compiler: type[SqlCypherCompiler], over: str, first: str
) -> None:
    # drivine4j#373: an operation over an aggregate's result was compiled with nothing bound, as the aggregate is, so
    # it wrote the aggregate out again at each level: max(n.title) + ': ' + max(n.first) + ' ' + max(n.last) compiled
    # to megabytes here (#235). A compiler of its own for each: the subquery a value is bound in is named by a count
    # a compiler keeps from one statement to the next.
    size, reference = (len(compiler("g").compile(it).sql) for it in (over, first))
    assert size < 120_000 and size < reference * 1.2, f"{over}: {size} characters, {reference} with a WITH first"


def test_an_aggregates_argument_compiled_only_to_see_whether_it_binds_declares_each_oracle_function_once() -> None:
    # Compiled to see, then again: a reduce() in it was declared twice, under one name or a name no query calls.
    z = "datetime('2024-03-15T10:00:00Z') + duration({minutes: x})"
    for statement in [
        "UNWIND range(1, 3) AS i RETURN sum(reduce(a = 0, x IN [i] | a + x))",
        f"UNWIND range(1, 3) AS i RETURN sum(reduce(a = 0, x IN [i] | a + ({z}).minute))",
        # And an operation over an aggregate's result, compiled to see whether it binds one (drivine4j#373).
        "UNWIND range(1, 3) AS i RETURN toLower(toLower(toString(reduce(a = max(i), x IN [1] | a + x)))) + 'x'",
    ]:
        sql = OracleCypherCompiler("g").compile(statement).sql
        declared = re.findall(r"FUNCTION (cy_\w+)\(", sql)
        assert declared and declared == list(dict.fromkeys(declared)), f"{statement}: {sql}"
        assert all(len(re.findall(rf"\b{name}\(", sql)) > 1 for name in declared), f"{statement}: {sql}"

    # Or an order term over one that reads an item declaring a function, compiled in the trial by the item's name: the
    # statement declares what it does without the term, each once (drivine4j#373).
    def declared_by(statement: str) -> list[str]:
        return re.findall(r"FUNCTION (cy_\w+)\(", OracleCypherCompiler("g").compile(statement).sql)

    grouped = "UNWIND range(1, 3) AS i RETURN reduce(a = 0, x IN [i] | a + x) AS r, count(*) AS c"
    sorted_ = declared_by(f"{grouped} ORDER BY toLower(toLower(toString(count(*)))) + toString(r)")
    assert sorted_ == list(dict.fromkeys(sorted_)), sorted_
    assert len(declared_by(grouped)) == len(sorted_), sorted_
    # Without the store's function, the query declares its own once, where the aggregate's argument binds a value or
    # not.
    for statement in [
        "UNWIND range(1, 3) AS i WITH i, [i, null] AS l, [i] AS m RETURN collect(l = m)",
        "UNWIND range(1, 3) AS i WITH i, [i, null] AS l, [i] AS m "
        "RETURN collect(l = m AND (datetime('2024-03-15T10:00:00Z') + duration({minutes: i})).minute > 0)",
    ]:
        sql = OracleCypherCompiler("g", nulls_equal_stored=False).compile(statement).sql
        assert len(re.findall(r"FUNCTION cy_nulls_equal\(", sql)) == 1, f"{statement}: {sql}"
        assert "THEN NULL ELSE (cy_nulls_equal(" in sql, f"{statement}: {sql}"
