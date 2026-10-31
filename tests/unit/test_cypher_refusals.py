"""
A statement the compiler cannot make SQL of, or that is wrong as Cypher is, is refused when it is compiled, with what is
wrong, on each store alike: before the store is sent anything. The statements and messages are the Cypher spec's, in
tests/cypher/refusals.yaml.
"""

from decimal import Decimal
from typing import Any

import pytest

from drivine import DrivineError
from drivine.cypher.ast.clause import Return
from drivine.cypher.ast.expression import Expression
from drivine.cypher.cypher_reader import read_cypher
from drivine.cypher.sql.oracle_cypher_compiler import OracleCypherCompiler
from drivine.cypher.sql.postgres_cypher_compiler import PostgresCypherCompiler
from drivine.cypher.sql.sql_cypher_compiler import SqlCypherCompiler, holds_subquery
from tests.cypher.cypher_spec import HELD_FAILURES, Case, expanded, failure_matches, held_for, held_passes, load

SPEC = load("refusals.yaml")

COMPILERS: dict[str, type[SqlCypherCompiler]] = {"postgres": PostgresCypherCompiler, "oracle": OracleCypherCompiler}


@pytest.mark.parametrize("store", COMPILERS)
@pytest.mark.parametrize("case", SPEC.cases, ids=[it.name for it in SPEC.cases])
def test_refusals(case: Case, store: str) -> None:
    for index, step in enumerate(case.steps):
        if not step.runs_on(store):
            continue
        issue = held_for("refusals.yaml", case, index, store)
        if issue is None:
            refuses(case, index, store)
            continue
        try:
            refuses(case, index, store)
        except HELD_FAILURES:
            # Held, as liberation-data/drivine-python#<issue> has it.
            continue
        pytest.fail(held_passes("refusals.yaml", case, index, store, issue))


def refuses(case: Case, index: int, store: str) -> None:
    """That the step at `index` of the case is refused on the store, as it says."""
    step = case.steps[index]
    where = f"{case.name}, step {index + 1}, on {store}: {step.cypher.strip()}"
    with pytest.raises(Exception) as refused:  # noqa: PT011
        COMPILERS[store]("g").compile(step.cypher, expanded(step.parameters))
    assert failure_matches(step, refused.value), f"{where}\nexpected: {step.error}\nactual:   {refused.value}"


@pytest.mark.parametrize("compiler", COMPILERS.values(), ids=COMPILERS.keys())
def test_to_string_of_a_list_or_a_map_the_statement_makes_is_refused_as_neo4j_refuses_it(
    compiler: type[SqlCypherCompiler],
) -> None:
    # drivine4j's #349: Postgres gave the list's JSON text, and Oracle null. The spec's refusals hold the rest.
    for cypher, kind in (("WITH [1, 2] AS l RETURN toString(l)", "list"), ("RETURN toString({a: 1})", "map")):
        with pytest.raises(DrivineError) as refused:
            compiler("g").compile(cypher)
        assert str(refused.value) == (
            f"toString() takes a number, a text, a truth value or a temporal value, not a {kind}, as in Neo4j."
        )
    # A value whose kind the statement leaves open is compiled, as Neo4j plans it.
    compiler("g").compile("UNWIND [1, 'a'] AS x RETURN toString(x)")
    compiler("g").compile("MATCH (n) RETURN toString(n.x)")


def test_two_lists_compared_in_a_write_or_a_reduce_step_are_refused_on_oracle_which_calls_no_declared_function() -> (
    None
):
    # drivine4j's #349: Oracle compares two lists by a function the query declares, which a procedure's queries do not
    # call.
    write = "CREATE (n:N {a: [1] < [2]})"
    with pytest.raises(
        DrivineError, match=r"^Two lists compared with <, <=, > or >= do not run on Oracle in a statement"
    ):
        OracleCypherCompiler("g").compile(write)
    PostgresCypherCompiler("g").compile(write)
    # Nor does a reduce()'s step, which is a function the query declares.
    folded = "RETURN reduce(acc = false, x IN [[1], [2]] | acc OR [1] < x)"
    with pytest.raises(DrivineError, match=r"^Two lists compared with <, <=, > or >= do not run on Oracle in the step"):
        OracleCypherCompiler("g").compile(folded)
    PostgresCypherCompiler("g").compile(folded)


@pytest.mark.parametrize("compiler", COMPILERS.values(), ids=COMPILERS.keys())
def test_a_date_written_as_a_text_that_is_no_date_is_refused_with_what_the_parser_says(
    compiler: type[SqlCypherCompiler],
) -> None:
    # Not in the spec: the message ends with Python's own text, which changes from one version to the next.
    with pytest.raises(DrivineError, match=r"^'2024-02-30' is no date: .+"):
        compiler("g").compile("RETURN date('2024-02-30')")


@pytest.mark.parametrize("compiler", COMPILERS.values(), ids=COMPILERS.keys())
@pytest.mark.parametrize(
    "written",
    [
        "localtime('10:00:00.1234567891')",
        "localdatetime('2024-01-01T10:00:00.1234567891')",
        "datetime('2024-01-01T10:00:00.1234567891Z')",
        "time('10:00:00.1234567891+01:00')",
    ],
)
def test_a_time_written_to_more_than_a_nanosecond_is_refused_as_on_a_graph_engine(
    compiler: type[SqlCypherCompiler], written: str
) -> None:
    # Python reads a fraction of any length, to the microsecond; Java, as drivine4j and Neo4j, to nine digits at most.
    with pytest.raises(DrivineError, match=r"^'[^']+' is no \w+: .+"):
        compiler("g").compile(f"RETURN {written}")


@pytest.mark.parametrize(
    "condition",
    [
        "size([i IN [1] WHERE COUNT { MATCH (n) RETURN n } > i]) > 0",
        "any(i IN [1] WHERE COUNT { MATCH (n) RETURN n } > i)",
        "reduce(s = 0, i IN [1] | s + COUNT { MATCH (n) RETURN n }) > 0",
        "size([(y)-->(z) WHERE COUNT { MATCH (n) RETURN n } > 0 | z]) > 0",
        "size([(y)-->({v: COUNT { MATCH (n) RETURN n }}) | y]) > 0",
        "(y)-->({v: COUNT { MATCH (n) RETURN n }})",
        "EXISTS { (y)-->(z) WHERE COUNT { MATCH (n) RETURN n } > 0 }",
        "EXISTS { (y)-->({v: COUNT { MATCH (n) RETURN n }}) }",
    ],
)
def test_a_subquery_anywhere_in_a_quantified_path_patterns_where_is_refused_on_oracle_where_a_variable_is_bound(
    condition: str,
) -> None:
    # Not in the spec, which holds a subquery at the top of the WHERE alone: one inside a comprehension, a quantifier, a
    # reduce or a pattern's properties or WHERE reads the row as much.
    with pytest.raises(
        DrivineError,
        match=r"^On Oracle, a subquery in a quantified path pattern that may read a variable from outside it is Cypher",
    ):
        OracleCypherCompiler("g").compile(f"MATCH (a:Person) ((x)-[:KNOWS]->(y) WHERE {condition})+ (b) RETURN b")


def test_a_subquery_in_a_quantified_path_patterns_properties_is_refused_on_oracle_where_a_variable_is_bound() -> None:
    with pytest.raises(
        DrivineError,
        match=r"^On Oracle, a subquery in a quantified path pattern that may read a variable from outside it is Cypher",
    ):
        OracleCypherCompiler("g").compile(
            "MATCH (a:Person) ((x {v: COUNT { MATCH (n) RETURN n }})-[:KNOWS]->(y))+ (b) RETURN b"
        )


def test_a_subquery_in_the_where_of_a_quantified_path_pattern_in_a_pattern_is_found() -> None:
    # Compiled on Oracle, the inner quantified path pattern refuses it of its own, so only the finding itself shows it.
    def returned(cypher: str) -> Expression:
        last = read_cypher(cypher).clauses[-1]
        assert isinstance(last, Return)
        return last.projection.items[0].expression

    subquery = "COUNT { MATCH (n) RETURN n } > 0"
    assert holds_subquery(returned(f"RETURN EXISTS {{ (y) ((c)-[:R]->(d) WHERE {subquery}){{1}} () }}"))
    assert not holds_subquery(returned("RETURN EXISTS { (y) ((c)-[:R]->(d) WHERE c.v > 0){1} () }"))


@pytest.mark.parametrize("compiler", COMPILERS.values(), ids=COMPILERS.keys())
def test_collect_distinct_beside_a_variable_no_other_item_is_outside_its_aggregates_is_refused_as_collect_is(
    compiler: type[SqlCypherCompiler],
) -> None:
    # Not in the spec yet: _distinct_first groups by each such variable, so it is refused before.
    with pytest.raises(DrivineError, match="names p outside its aggregates"):
        compiler("g").compile("MATCH (p:Person) RETURN p.born, collect(DISTINCT p.name) + p.name")


@pytest.mark.parametrize("compiler", COMPILERS.values(), ids=COMPILERS.keys())
@pytest.mark.parametrize(
    ("cypher", "parameters"),
    [
        ("RETURN round(1234.5, -2)", {}),
        ("RETURN round(1234.5, -1.2, 'UP')", {}),
        ("RETURN round(1234.5, -$p)", {"p": 2}),
        ("RETURN round(1234.5, $p)", {"p": -2.5}),
        ("RETURN round(1234.5, $p)", {"p": Decimal("-1.2")}),
        ("RETURN round(1234.5, $p)", {"p": float("-inf")}),
        ("RETURN round(1234.5, 2147483648)", {}),
        ("RETURN round(1234.5, $p, 'HALF_UP')", {"p": float("inf")}),
        ("RETURN round(1234.5, -9223372036854775808)", {}),
    ],
)
def test_round_compiles_a_precision_below_0_or_past_2147483647_to_fail_on_the_row_that_rounds_a_number_by_it(
    compiler: type[SqlCypherCompiler], cypher: str, parameters: dict[str, Any]
) -> None:
    # Neo4j reads the precision only when it rounds (drivine4j#305), so none is refused when compiling, in both forms.
    sql = compiler("g").compile(cypher, parameters).sql
    assert "round() takes a precision from 0 to 2147483647, as in Neo4j." in sql, sql


@pytest.mark.parametrize("compiler", COMPILERS.values(), ids=COMPILERS.keys())
@pytest.mark.parametrize(
    ("cypher", "parameters"),
    [
        ("RETURN [round(1234.5, -0.5), round(1234.5, $p, 'HALF_UP')]", {"p": -0.9}),
        ("RETURN [round(1234.5, 2147483647), round(1234.5, $p, 'UP')]", {"p": 2147483647.9}),
        ("RETURN [round(1234.5, $p), round(1234.5, $p, 'UP')]", {"p": float("nan")}),
    ],
)
def test_round_takes_a_given_precision_from_0_to_2147483647_toward_zero_and_nan_as_0_with_nothing_to_fail(
    compiler: type[SqlCypherCompiler], cypher: str, parameters: dict[str, Any]
) -> None:
    sql = compiler("g").compile(cypher, parameters).sql
    assert "round() takes a precision" not in sql, sql


@pytest.mark.parametrize("compiler", COMPILERS.values(), ids=COMPILERS.keys())
@pytest.mark.parametrize(
    "n",
    [
        pytest.param(10**400, id="10**400"),
        pytest.param(-(10**400), id="-10**400"),
        2147483648,
        float("inf"),
        float("-inf"),
        Decimal("-1.5"),
        Decimal("2147483648.5"),
        Decimal("Infinity"),
    ],
)
def test_a_bound_length_out_of_range_is_refused_however_large_and_of_whatever_kind(
    compiler: type[SqlCypherCompiler], n: Any
) -> None:
    # Not in the spec yet (#143): a Python int has no bound, and is past the greatest as a Java long is.
    with pytest.raises(DrivineError, match=r"^left\(\) takes a length from 0 to 2147483647, as in Neo4j\.$"):
        compiler("g").compile("RETURN left('hello', $n)", {"n": n})


@pytest.mark.parametrize("compiler", COMPILERS.values(), ids=COMPILERS.keys())
@pytest.mark.parametrize(
    ("n", "cut"), [(float("nan"), 0), (Decimal("2.7"), 2), (Decimal("-0.5"), 0), (2147483647, 2147483647)]
)
def test_a_bound_length_is_cut_toward_zero_as_java_casts_it_nan_to_0(
    compiler: type[SqlCypherCompiler], n: Any, cut: int
) -> None:
    # Not in the spec yet (#143): a Decimal is read as the number it is, and NaN as Java casts a double to a long.
    assert f"CAST({cut} AS INTEGER)" in compiler("g").compile("RETURN left('hello', $n)", {"n": n}).sql


@pytest.mark.parametrize("compiler", COMPILERS.values(), ids=COMPILERS.keys())
@pytest.mark.parametrize(
    ("n", "cut"),
    [
        (1.0e300, 9223372036854775807),
        (-1.0e300, -9223372036854775808),
        pytest.param(10**400, 9223372036854775807, id="10**400"),
        pytest.param(-(10**400), -9223372036854775808, id="-10**400"),
        (Decimal("1E+400"), 9223372036854775807),
        (Decimal("-Infinity"), -9223372036854775808),
    ],
)
def test_a_bound_length_past_a_long_beside_a_text_from_data_is_cut_to_the_greatest_or_least_long(
    compiler: type[SqlCypherCompiler], n: Any, cut: int
) -> None:
    # Not in the spec yet (#169): as Java casts a double to a long, so no store is given a literal past its number.
    sql = compiler("g").compile("MATCH (n:Count) RETURN left(n.t, $n)", {"n": n}).sql
    assert f"THEN {cut} END" in sql and str(abs(cut) + 1) not in sql


@pytest.mark.parametrize("compiler", COMPILERS.values(), ids=COMPILERS.keys())
@pytest.mark.parametrize("p", [-0.9, Decimal("-0.5"), float("nan")])
def test_round_takes_a_precision_above_minus_1_as_0_places_and_compiles_one_that_is_nan(
    compiler: type[SqlCypherCompiler], p: Any
) -> None:
    # -0.5 and -0.9 cut toward zero are 0 places; NaN is no precision below 0, and is left to the store.
    assert compiler("g").compile("RETURN [round(1234.5, -0.5), round(1234.5, $p, 'HALF_UP')]", {"p": p}).sql


def refusal(compiler: type[SqlCypherCompiler], cypher: str) -> str | None:
    """Why the compiler refuses the statement, or None where it compiles it."""
    try:
        compiler("g").compile(cypher)
        return None
    except Exception as e:  # noqa: BLE001
        return str(e)


@pytest.mark.parametrize("compiler", COMPILERS.values(), ids=COMPILERS.keys())
@pytest.mark.parametrize(
    ("cypher", "name"),
    [
        # A CALL's brackets import only variables of its row; a nested CALL imports from the outer CALL's scope.
        ("MATCH (a) CALL (x) { RETURN 1 AS n } RETURN n", "x"),
        ("MATCH (a), (b) CALL (a) { CALL (b) { MATCH (b)-->(d) RETURN d } RETURN d.x AS x } RETURN x", "b"),
        # A procedure's call sees the whole row, which holds no a after a WITH that does not pass it on.
        ("MATCH (a) WITH 1 AS x CALL db.labels() YIELD label WHERE label = a.kind RETURN label", "a"),
        # A CALL holding a randomUUID() reads the UUID made for its row, and still imports only what it names.
        ("UNWIND [1] AS i CALL { RETURN toString(i) + randomUUID() AS u } RETURN u", "i"),
        ("UNWIND [1] AS i CALL (x) { RETURN randomUUID() AS u } RETURN u", "x"),
        # A variable quoted as the compiler names its own is imported as any other is.
        ("UNWIND [1] AS `#anew9` CALL () { RETURN `#anew9` AS x } RETURN x", "#anew9"),
    ],
)
def test_a_call_sees_only_the_variables_of_the_row_it_imports_and_a_bracketed_name_not_of_the_row_is_refused(
    compiler: type[SqlCypherCompiler], cypher: str, name: str
) -> None:
    # drivine4j's #410 (#294 here). A CALL saw every variable of its row, so a name it does not import joined the row's.
    with pytest.raises(DrivineError) as refused:
        compiler("g").compile(cypher)
    assert str(refused.value) == (
        f"{name} is not defined: a variable is bound by a pattern, a WITH or an UNWIND before it is used."
    )


@pytest.mark.parametrize("compiler", COMPILERS.values(), ids=COMPILERS.keys())
@pytest.mark.parametrize(
    "cypher",
    [
        # A name a CALL does not import is a new variable in it, of any kind.
        "MATCH (a) CALL { MATCH ()-[a]->() RETURN 1 AS x } RETURN a, x",
        "MATCH (a) CALL () { MATCH ()-[a]->() RETURN 1 AS x } RETURN a, x",
        "MATCH (a), (b) CALL (a) { MATCH ()-[b]->() RETURN 1 AS x } RETURN x",
        "MATCH (a) CALL { WITH a WITH 1 AS one MATCH ()-[a]->() RETURN 1 AS x } RETURN x",
        "MATCH (a) CALL () { CREATE (a) RETURN 1 AS x } RETURN x",
        "MATCH (a) CALL { WITH a MATCH (a)-->(b) RETURN b } MATCH (b)-->() RETURN b",
        "MATCH (a) CALL (a) { MATCH (a)-->(b) RETURN b } MATCH (b)-->() RETURN b",
        "MATCH (a) CALL (a) { CREATE (a)-[:R]->(b) RETURN b } RETURN b",
        # A procedure's call imports each variable of the row, so its WHERE reads them.
        "MATCH (a) CALL db.labels() YIELD label WHERE label = a.kind RETURN label",
    ],
)
def test_a_name_a_call_does_not_import_is_a_new_variable_in_it(compiler: type[SqlCypherCompiler], cypher: str) -> None:
    # drivine4j's #410 (#294 here): each was refused, as a variable bound already, or as one used as two kinds.
    assert refusal(compiler, cypher) is None


IMPORTING_WITH = (
    "The importing WITH of a CALL { … } names variables of the row alone, as in Cypher: no alias, expression, "
    "DISTINCT, WHERE, ORDER BY, SKIP or LIMIT. Import them with CALL (a, b) { … }, and work on them in a WITH after."
)


@pytest.mark.parametrize("compiler", COMPILERS.values(), ids=COMPILERS.keys())
@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (a), (b) CALL { WITH a, b AS c MATCH (a)-->(c) RETURN c.x AS x } RETURN x",
        "MATCH (a) CALL { WITH a AS c MATCH (c)-->(d) RETURN d.x AS x } RETURN x",
        "MATCH (a) CALL { WITH a WHERE a.x > 1 RETURN count(*) AS n } RETURN a, n",
        "MATCH (a) CALL { WITH a ORDER BY a.x LIMIT 1 MATCH (a)-->(d) RETURN d.x AS x } RETURN x",
        "MATCH (a) CALL { WITH a SKIP 0 RETURN 1 AS x } RETURN x",
        "MATCH (a) CALL { WITH DISTINCT a MATCH (a)-->(d) RETURN d.x AS x } RETURN x",
        "MATCH (a) CALL { WITH a, 1 AS one MATCH (a)-->(d) RETURN d.x + one AS x } RETURN x",
        "MATCH (a) CALL { WITH * WHERE true RETURN 1 AS x } RETURN x",
        "MATCH (a) CALL { UNWIND [1] AS x RETURN x UNION WITH a WHERE true RETURN 1 AS x } RETURN x",
        "MATCH (a) CALL { WITH a WHERE a.x > 1 SET a.y = 1 } RETURN a",
        # One holding a randomUUID() too, whose query reads a UUID made for the row before its WITH.
        "MATCH (a) CALL { WITH a AS c RETURN c.x + randomUUID() AS x } RETURN x",
    ],
)
def test_a_calls_leading_with_that_imports_names_variables_of_the_row_alone_as_neo4j_has_it(
    compiler: type[SqlCypherCompiler], cypher: str
) -> None:
    # drivine4j's #410 (#294 here): Neo4j refuses an importing WITH with anything more (its 42I28), which was compiled
    # as any WITH.
    with pytest.raises(DrivineError) as refused:
        compiler("g").compile(cypher)
    assert str(refused.value) == IMPORTING_WITH


@pytest.mark.parametrize("compiler", COMPILERS.values(), ids=COMPILERS.keys())
@pytest.mark.parametrize(
    "cypher",
    [
        # A leading WITH that names no variable of the row as it is imports nothing, and is any WITH; so is one of a
        # CALL with brackets.
        "MATCH (a) CALL { WITH 1 AS one WHERE one > 0 RETURN one } RETURN one",
        "MATCH (a) CALL { WITH DISTINCT 1 AS one RETURN one } RETURN one",
        "MATCH (a) CALL { WITH a AS a RETURN 1 AS one } RETURN one",
        "MATCH (a) CALL (a) { WITH a WHERE a.x > 1 RETURN a.x AS x } RETURN x",
        "MATCH (a) CALL (a) { WITH DISTINCT a, 1 AS one ORDER BY a.x LIMIT 1 RETURN one } RETURN one",
    ],
)
def test_a_leading_with_that_imports_nothing_or_follows_brackets_is_any_with(
    compiler: type[SqlCypherCompiler], cypher: str
) -> None:
    assert refusal(compiler, cypher) is None


@pytest.mark.parametrize("compiler", COMPILERS.values(), ids=COMPILERS.keys())
def test_a_compiler_compiling_again_keeps_none_of_the_names_it_made_for_the_statement_before(
    compiler: type[SqlCypherCompiler],
) -> None:
    # The first makes #anew2 for its CALL's UUID; the second quotes a variable so. Kept from the first, the second's
    # star left it out, and gave no item; its texts kept from the second, the first's names were made otherwise.
    made = "UNWIND [1] AS i CALL { RETURN toUpper(randomUUID()) AS u } RETURN u"
    quoted = "UNWIND [1, 2] AS `#anew2` WITH * RETURN *"
    for first, second in [(made, quoted), (quoted, made)]:
        again = compiler("g")
        again.compile(first)
        assert again.compile(second).sql == compiler("g").compile(second).sql


@pytest.mark.parametrize("compiler", COMPILERS.values(), ids=COMPILERS.keys())
@pytest.mark.parametrize(
    ("cypher", "parameters", "message"),
    [
        # A parameter's value is known as the statement is compiled, once for each call: a step of 0 would never end,
        # and an index given of another kind is refused as one written is.
        ("RETURN range(2, 8, $s)", {"s": 0}, "range() takes a step other than 0, as in Neo4j."),
        ("RETURN range(2, 8, $s)", {"s": 0.0}, "range() takes a step other than 0, as in Neo4j."),
        # A Decimal is given as the number it is, as a bigint is in Drivine for TypeScript.
        ("RETURN range(2, 8, $s)", {"s": Decimal(0)}, "range() takes a step other than 0, as in Neo4j."),
        ("WITH [1] AS l RETURN l[$k]", {"k": "a"}, "A list is indexed by an integer, not a text, as in Neo4j."),
        ("WITH {a: 1} AS m RETURN m[$k]", {"k": 1}, "A map is indexed by a text, not an integer, as in Neo4j."),
        ("WITH [1] AS l RETURN l[$k]", {"k": True}, "A list is indexed by an integer, not a truth value, as in Neo4j."),
        # A value carried across a write is of the kind the statement made it.
        ("WITH 1 AS x CREATE (n) RETURN x.num", {}, "An integer has no properties."),
        # A variable each query of a UNION in a CALL makes of the same kind is of that kind.
        ("CALL { RETURN 1 AS x UNION ALL RETURN 2 AS x } RETURN x.num", {}, "An integer has no properties."),
    ],
)
def test_a_value_the_statement_writes_as_one_of_a_kind_an_operator_or_a_function_does_not_take_is_refused(
    compiler: type[SqlCypherCompiler], cypher: str, parameters: dict[str, Any], message: str
) -> None:
    # drivine4j's #420 (#448 there), and Drivine for TypeScript's #482: the spec's refusals hold the statements written;
    # these are beside them.
    with pytest.raises(DrivineError) as refused:
        compiler("g").compile(cypher, parameters)
    assert str(refused.value) == message


@pytest.mark.parametrize("compiler", COMPILERS.values(), ids=COMPILERS.keys())
@pytest.mark.parametrize(
    ("cypher", "parameters"),
    [
        ("MATCH (n) WHERE n.flag AND NOT n.other RETURN n", {}),
        ("RETURN $p AND true", {"p": 1}),
        ("MATCH (n) RETURN n.x.y, n.x[0], 1 IN n.x, range(0, n.x), properties(n.x)", {}),
        ("UNWIND [{a: 1}] AS m RETURN m.a, m['a']", {}),
        ("RETURN NOT null, null XOR true, null.num, null[0], [1, 2, 3][null], 1 IN null, properties(null)", {}),
        ("WITH {a: 1} AS m, 'a' AS k RETURN m.a, m[k], properties(m), [1, 2][-1], range(0, 10, -2)", {}),
        ("MATCH (a) SET a.tags = ['x', 'y'], a.n = -1", {}),
        # A variable each query of a UNION makes of another kind is of none known.
        ("CALL { RETURN 1 AS x UNION RETURN {a: 1} AS x } RETURN x.a", {}),
        # A variable a later WITH, a comprehension, a quantifier or a reduce() binds again is of the kind that one
        # makes it.
        ("WITH 1 AS x WITH {a: 1} AS x RETURN x.a", {}),
        (
            "WITH 1 AS x RETURN [x IN [{a: 1}] | x.a], [x IN [true] WHERE x], any(x IN [true] WHERE x), "
            "reduce(x = true, y IN [true] | x AND y)",
            {},
        ),
        ("WITH 1 AS x FOREACH (x IN [{a: 1}] | CREATE ({v: x.a}))", {}),
        # A CASE, a coalesce(), a list joined, an item of a list or a map written, or a parameter, is of no kind known.
        (
            "WITH CASE WHEN true THEN {a: 1} ELSE 1 END AS x, coalesce(null, {a: 1}) AS y, [1] + [{a: 1}] AS z "
            "RETURN x.a, y.a, z[1]",
            {},
        ),
        ("RETURN [{a: 1}][0].a, [true][0] AND true, {a: true}.a AND true, {a: [1]}['a'][0]", {}),
        ("WITH $p AS x RETURN x.a, x AND true, x[0], 1 IN x, properties(x)", {"p": {"a": 1}}),
        ("WITH [1] AS l, {a: 1} AS m RETURN l[$i], m[$k]", {"i": 0, "k": "a"}),
        ("WITH {a: 1} AS m CREATE (n) RETURN m.a", {}),
        # A name a WITH or an UNWIND binds again after a write is of the kind that one makes it.
        ("WITH 1 AS x CREATE (n {v: x}) WITH n AS x RETURN x.v", {}),
        ("WITH 1 AS x CREATE (n {v: x}) WITH n.v AS x RETURN x.a", {}),
        ("WITH 1 AS x CREATE (n) WITH {a: 1} AS x CREATE (m) RETURN x.a", {}),
        ("WITH 1 AS x CREATE (n) WITH 1 AS y UNWIND [{a: 1}] AS x RETURN x.a", {}),
    ],
)
def test_a_value_whose_kind_is_known_only_when_the_row_is_read_is_compiled_as_neo4j_plans_it(
    compiler: type[SqlCypherCompiler], cypher: str, parameters: dict[str, Any]
) -> None:
    # A property, a parameter or an unwound item, and null and the kinds each takes.
    compiler("g").compile(cypher, parameters)


@pytest.mark.parametrize("compiler", COMPILERS.values(), ids=COMPILERS.keys())
@pytest.mark.parametrize(
    "cypher",
    [
        # Neo4j takes each of these: a node bound before an OPTIONAL MATCH or a MERGE that joins it up, a name a WITH,
        # a UNION, a pattern comprehension, an EXISTS or a CALL that imports nothing leaves to a new variable, a list a
        # variable-length relationship matches, a value whose kind the statement leaves open, and a variable bound
        # already that a CREATE or a MERGE in a FOREACH names.
        "MATCH (a) OPTIONAL MATCH (a)-->(b) RETURN b",
        "MATCH (a) OPTIONAL MATCH (a) RETURN a",
        "OPTIONAL MATCH (a) MATCH (a)-->() RETURN a",
        "MATCH (a) MERGE (a)-[:R]->(b)",
        "MATCH (a) MERGE (b)-[:R]->(a)",
        "MATCH (a) MERGE (a)-[:R]->(a)",
        "CREATE (a) MERGE (a)-[:R]->(b)",
        "MATCH (a)-[r]->(b) MERGE (a)-[s:T]->(b)",
        "MATCH (n) CREATE (n)-[:R]->(n)",
        "CREATE (n)-[:T1]->(), (n)-[:T2]->()",
        "MATCH (a) CREATE (b), (a)-[:R]->(b)",
        "MATCH (n) FOREACH (x IN [1] | CREATE (n)-[:R]->())",
        "MATCH (n) FOREACH (x IN [1] | CREATE (n))",
        "FOREACH (x IN [1] | CREATE (n), (n))",
        "MATCH (a) FOREACH (x IN [1] | MERGE (a)-[:R]->())",
        "MATCH (a) WITH a SKIP 0 MERGE (a)-[:R]->()",
        "MATCH (a) CALL { CREATE (a) RETURN 1 AS x } RETURN x",
        "MATCH (a)-[r*]->() WITH r MATCH ()-[r*]->() RETURN r",
        "MATCH (a) RETURN [(a)-->(a) | 1] AS l",
        "WITH 1 AS p RETURN [p = ()-->() | length(p)] AS l",
        "MATCH (a), (a)-->() RETURN a",
        "MATCH (n) WITH n MATCH (n:L) RETURN n",
        "MATCH (a) WITH a AS b, a AS c MATCH (b)-->(c) RETURN b",
        "MATCH ()-[a]->() WITH 1 AS x MATCH (a) RETURN a",
        "MATCH ()-[r]->() RETURN r UNION ALL MATCH (r) RETURN r",
        "MATCH (a) RETURN [a IN [1] | a] AS x",
        "MATCH (n) RETURN [p = (n)-->() | length(p)] AS x",
        "MATCH (a) WITH [(a)-[r]->() | r] AS rs MATCH ()-[r]->() RETURN r",
        "MATCH (a) WHERE EXISTS { MATCH (a)-[r]->() } MATCH ()-[r]->() RETURN r",
        "MATCH (a) CALL { WITH a MATCH (a)-->(b) RETURN b } MATCH (b)-->() RETURN b",
        "MATCH (a) CALL (a) { MATCH (a)-->(b) RETURN b } MATCH (b)-->() RETURN b",
        "MATCH (a) CALL (a) { CREATE (a)-[:R]->(b) RETURN b } RETURN b",
        "MATCH ()-[n]->(m) FOREACH (n IN [m] | CREATE (n)-[:R]->())",
        "MATCH (a) WITH a, 1 AS b ORDER BY size([(a)-->(x) | x]) RETURN b",
        "MATCH (a) MATCH p = (a)-->() RETURN nodes(p)",
        "MATCH ()-[r*]->() RETURN size(r)",
        "WITH [] AS r MATCH ()-[r*]-() RETURN r",
        "MATCH (a) WITH collect(a) AS ns UNWIND ns AS n MATCH (n)-->(m) RETURN m",
        # What a CALL's brackets import stays in scope, of its kind, after a WITH inside it (drivine4j#464).
        "MATCH (a) CALL (a) { WITH 1 AS one MATCH (a)-->(b) RETURN b } RETURN b",
    ],
)
def test_a_variable_bound_already_is_compiled_where_neo4j_takes_it(
    compiler: type[SqlCypherCompiler], cypher: str
) -> None:
    # drivine4j's #451: the spec's refusals hold the statements Neo4j refuses; these are beside them.
    compiler("g").compile(cypher)


_BOUND_BEFORE = "A relationship variable bound before its pattern is Cypher, and is not compiled to SQL yet."


@pytest.mark.parametrize("compiler", COMPILERS.values(), ids=COMPILERS.keys())
@pytest.mark.parametrize(
    ("cypher", "message"),
    [
        (
            "MATCH (a) CALL (*) { CREATE (a) } RETURN 1",
            "a is bound already, so a CREATE makes no node of it, as in Neo4j.",
        ),
        # Neo4j takes these, and the compiler refuses them for what it does not compile yet, as before. So it refuses a
        # relationship bound before a CREATE or a MERGE that makes it again, as the spec has it, which Neo4j refuses as
        # bound already.
        ("MATCH ()-[r]->() WITH r MATCH ()-[r]->() RETURN r", _BOUND_BEFORE),
        ("MATCH (a)-[r]->() RETURN EXISTS { (a)-[r]->() } AS e", _BOUND_BEFORE),
        ("MATCH (a)-[r]->(b) MERGE (a)-[r]->(b)", _BOUND_BEFORE),
        ("MATCH ()-[r]->() FOREACH (x IN [1] | CREATE ()-[r:T]->())", _BOUND_BEFORE),
        (
            "MATCH (a) CALL { WITH a RETURN a AS b UNION WITH a MATCH ()-[b]->() RETURN b } RETURN b",
            "A UNION in CALL { … } whose queries return values of different kinds is Cypher, and is not compiled to "
            "SQL yet.",
        ),
        (
            "MATCH p = ()-->() RETURN [p = ()-->() | p] AS x",
            "A path is not a value drivine gives the same on each store: use nodes(p), relationships(p) and length(p).",
        ),
    ],
)
def test_a_variable_used_as_two_kinds_or_made_where_it_is_bound_already_is_refused_as_neo4j_refuses_it(
    compiler: type[SqlCypherCompiler], cypher: str, message: str
) -> None:
    with pytest.raises(DrivineError) as refused:
        compiler("g").compile(cypher)
    assert str(refused.value) == message


@pytest.mark.parametrize("compiler", COMPILERS.values(), ids=COMPILERS.keys())
@pytest.mark.parametrize("unit", ["fortnight", "__class__", "keys"])
def test_a_truncation_to_a_unit_it_has_no_sql_for_names_each_unit_it_takes(
    compiler: type[SqlCypherCompiler], unit: str
) -> None:
    units = (
        "millennium, century, decade, year, weekYear, quarter, month, week, day, hour, minute, second, millisecond or "
        "microsecond"
    )
    with pytest.raises(DrivineError) as refused:
        compiler("g").compile(f"RETURN date.truncate('{unit}', date('2024-03-15'))")
    assert str(refused.value) == f"date.truncate() truncates to {units}, not {unit}."


@pytest.mark.parametrize("compiler", COMPILERS.values(), ids=COMPILERS.keys())
@pytest.mark.parametrize(
    ("cypher", "message"),
    [
        ("RETURN datetime.truncate('day', datetime(), {timezone: '+19:00'})", "'+19:00' is no time zone."),
        ("RETURN datetime.truncate('day', datetime(), {timezone: '+05:60'})", "'+05:60' is no time zone."),
        (
            "RETURN datetime.truncate('day', datetime(), {timezone: 'UTC'})",
            "The time zone 'UTC' is Cypher, and is not compiled to SQL yet.",
        ),
        (
            "RETURN date.truncate('year', date(), {quarter: 2})",
            "date.truncate() of a map with quarter is Cypher, and is not compiled to SQL yet.",
        ),
        ("RETURN time.truncate('hour', time(), {year: 2000})", "time.truncate() takes no field year."),
        ("RETURN date.truncate('month', date(), {day: -1})", "date.truncate() takes a day from 1 to 31, not -1."),
        (
            "RETURN date.truncate('month', date(), {day: true})",
            "date.truncate() takes a whole number for day, not true.",
        ),
    ],
)
def test_a_truncations_fields_past_what_the_spec_holds_are_refused_as_neo4j_refuses_them(
    compiler: type[SqlCypherCompiler], cypher: str, message: str
) -> None:
    # drivine4j's #468, and Drivine for TypeScript's #482: Neo4j takes a time zone as an offset alone or by name, which
    # no store's text holds yet, and the fields of a week or a quarter, which no store has SQL for yet.
    with pytest.raises(DrivineError) as refused:
        compiler("g").compile(cypher)
    assert str(refused.value) == message


@pytest.mark.parametrize("compiler", COMPILERS.values(), ids=COMPILERS.keys())
def test_a_truncations_fields_given_as_an_expression_a_negated_number_utc_or_none_are_compiled(
    compiler: type[SqlCypherCompiler],
) -> None:
    for cypher in (
        "MATCH (n) RETURN date.truncate('month', date(), {day: n.day, year: -1})",
        "RETURN datetime.truncate('day', datetime(), {timezone: '-00:00'}), "
        "datetime.truncate('day', datetime(), {timezone: '+18:00'})",
        "RETURN localdatetime.truncate('day', localdatetime(), {}), localtime.truncate('hour', localtime(), null)",
        # A float negated is an expression, as drivine4j and Drivine for TypeScript read it: only a whole number
        # negated is written.
        "RETURN date.truncate('month', date(), {day: -2.0})",
    ):
        compiler("g").compile(cypher)


@pytest.mark.parametrize("compiler", COMPILERS.values(), ids=COMPILERS.keys())
@pytest.mark.parametrize(
    ("cypher", "message"),
    [
        # A year past Java's, as Neo4j's, and one whose date would be past Java's last.
        (
            "RETURN date({year: 4294969319, week: 1})",
            "date() takes no year 4294969319: Invalid value for Year (valid values -999999999 - 999999999): 4294969319",
        ),
        (
            "RETURN date({year: -1000000000, ordinalDay: 1})",
            "date() takes no year -1000000000: Invalid value for Year (valid values -999999999 - 999999999): "
            "-1000000000",
        ),
        (
            "RETURN date({year: 999999999, week: 53, dayOfWeek: 7})",
            "date() takes no year 999999999: Invalid value for EpochDay (valid values -365243219162 - 365241780471): "
            "365241780480",
        ),
        ("RETURN date({year: 2023, week: true})", "date() takes a whole number for week, not true."),
        ("RETURN date({year: 2023, quarter: 2.0})", "date() takes a whole number for quarter, not 2.0."),
        (
            "RETURN date({ordinalDay: 2})",
            "date() of an ordinal day without a year is Cypher, and is not compiled to SQL yet.",
        ),
        (
            "RETURN date({date: date('2024-03-15'), week: 2})",
            "date() of a map with date is Cypher, and is not compiled to SQL yet.",
        ),
        (
            "RETURN datetime({year: 1984, week: 10, timezone: 'Europe/Stockholm'})",
            "The time zone 'Europe/Stockholm' is Cypher, and is not compiled to SQL yet.",
        ),
        ("RETURN localtime({hour: 12, dayOfQuarter: 2})", "localtime() takes no field dayOfQuarter."),
    ],
)
def test_a_temporal_map_of_the_calendars_fields_past_what_the_spec_holds_is_refused_as_neo4j_refuses_it(
    compiler: type[SqlCypherCompiler], cypher: str, message: str
) -> None:
    # #312, drivine4j's #495: the spec's "Temporal map of calendar fields Neo4j does not take" holds the rest. Neo4j
    # makes an ordinal day of no year one of the year 0, which no store's date reaches, and a zone by name no store's
    # text holds yet.
    with pytest.raises(DrivineError) as refused:
        compiler("g").compile(cypher)
    assert str(refused.value) == message


@pytest.mark.parametrize("compiler", COMPILERS.values(), ids=COMPILERS.keys())
def test_a_temporal_map_of_the_calendars_fields_that_runs_on_or_that_an_expression_gives_is_compiled(
    compiler: type[SqlCypherCompiler],
) -> None:
    # #312, drivine4j's #495: week 53 of a year of 52 weeks and a day of the quarter past its last run on, and what an
    # expression gives is the store's to check.
    for cypher in (
        "RETURN date({year: 2019, week: 53}), date({year: 2023, quarter: 1, dayOfQuarter: 92}), "
        "date({year: 2024, ordinalDay: 366})",
        "WITH 54 AS w RETURN date({year: 2020, week: w}), datetime({year: $y, ordinalDay: 366, timezone: '+01:00'})",
        "RETURN date({year: -1, week: -1 + 2, dayOfWeek: 7}), datetime({year: 1984, week: 10, timezone: 'Z'})",
    ):
        compiler("g").compile(cypher, {"y": 2023})


def _in_where(aggregate: str) -> str:
    return (
        f"{aggregate} aggregates, and a WHERE holds no aggregate unless its WITH aggregates, as in Neo4j: aggregate in "
        "a WITH first, and filter what it gives in that WITH's WHERE, as in WITH x, count(*) AS c WHERE c > 1."
    )


def _undefined(variable: str) -> str:
    return f"{variable} is not defined: a variable is bound by a pattern, a WITH or an UNWIND before it is used."


@pytest.mark.parametrize("compiler", COMPILERS.values(), ids=COMPILERS.keys())
@pytest.mark.parametrize(
    ("cypher", "message"),
    [
        ("UNWIND [1, 2] AS i WITH i WHERE sum(i) > 1 RETURN i", _in_where("sum()")),
        ("UNWIND [1, 2] AS i WITH i WHERE i > 1 AND count(*) > 0 RETURN i", _in_where("count(*)")),
        ("MATCH (n) WHERE count(*) > 1 RETURN n", _in_where("count(*)")),
        ("MATCH (a) WHERE a.x IN collect(a.y) RETURN a", _in_where("collect()")),
        ("MATCH (n) RETURN [(n)-->(m) WHERE count(m) > 1 | m] AS ms", _in_where("count()")),
        ("UNWIND [[1, 2]] AS l RETURN all(x IN l WHERE sum(x) > 1) AS a", _in_where("sum()")),
        ("UNWIND [[1, 2]] AS l RETURN [x IN l WHERE count(x) > 1 | x] AS a", _in_where("count()")),
        ("MATCH (a) ((x)-[:R]->(y) WHERE count(x) > 1)+ (b) RETURN a", _in_where("count()")),
        ("MATCH (n) WHERE EXISTS { (n)-->(m) WHERE count(m) > 1 } RETURN n", _in_where("count()")),
        ("MATCH (n) WHERE COUNT { (n)-->(m) WHERE count(m) > 1 } > 0 RETURN n", _in_where("count()")),
        ("MATCH (n) RETURN COUNT { MATCH (n)-->(m) WHERE count(m) > 1 RETURN m } AS c", _in_where("count()")),
        ("MATCH (n) RETURN COUNT { MATCH (n)-->(m) WITH m WHERE count(m) > 1 RETURN m } AS c", _in_where("count()")),
        ("UNWIND [1, 2] AS i CALL (i) { WITH i WHERE sum(i) > 1 RETURN i AS j } RETURN j", _in_where("sum()")),
        ("UNWIND [1, 2] AS i CALL { WITH i MATCH (n) WHERE max(n.x) > i RETURN n } RETURN n", _in_where("max()")),
        (
            "UNWIND [1, 2] AS i CALL (i) { CALL (i) { UNWIND [1, 2] AS x WITH x WHERE sum(x) > i RETURN x } RETURN x } "
            "RETURN x",
            _in_where("sum()"),
        ),
        (
            "MATCH (p:Person) RETURN p.name AS n UNION MATCH (m:Movie) WHERE count(*) > 1 RETURN m.title AS n",
            _in_where("count(*)"),
        ),
        ("UNWIND [x IN [1, 2] WHERE count(x) > 0 | x] AS y RETURN y", _in_where("count()")),
        ("MATCH (n) SET n.v = [x IN [1, 2] WHERE sum(x) > 0 | x]", _in_where("sum()")),
        ("MERGE (n:N) ON CREATE SET n.v = [x IN [1, 2] WHERE min(x) > 0 | x]", _in_where("min()")),
        ("MERGE (n:N) ON MATCH SET n.v = [x IN [1, 2] WHERE min(x) > 0 | x]", _in_where("min()")),
        ("MATCH (n) DELETE [x IN [n] WHERE count(x) > 0 | x][0]", _in_where("count()")),
        ("UNWIND [1, 2] AS i RETURN i SKIP size([x IN [1] WHERE sum(x) > 0])", _in_where("sum()")),
        ("CREATE (n:N {v: [x IN [1, 2] WHERE avg(x) > 0 | x]})", _in_where("avg()")),
        ("FOREACH (i IN [1] | CREATE (:N {v: [x IN [1, 2] WHERE max(x) > 0 | x]}))", _in_where("max()")),
        # #313, drivine4j's #497: a WITH whose only aggregate is in its WHERE groups nothing; in the WHERE of one that
        # groups, a comprehension's, a quantifier's or a pattern's own WHERE or body holds no aggregate either.
        ("UNWIND [1, 1, 2] AS i WITH i WHERE count(*) > 1 RETURN i", _in_where("count(*)")),
        ("UNWIND [1, 1, 2] AS i WITH * WHERE count(*) > 1 RETURN i", _in_where("count(*)")),
        (
            "UNWIND [1, 1, 2] AS i WITH i, count(*) AS c WHERE all(x IN [1] WHERE count(*) > x) RETURN i, c",
            _in_where("count(*)"),
        ),
        (
            "UNWIND [1, 1, 2] AS i WITH i, count(*) AS c WHERE size([x IN [1] WHERE count(*) > x | x]) > 0 RETURN i, c",
            _in_where("count(*)"),
        ),
        (
            "UNWIND [1, 1, 2] AS i WITH i, count(*) AS c WHERE [x IN [1] | x + count(*)][0] > 2 RETURN i, c",
            _in_where("count(*)"),
        ),
        (
            "UNWIND [1, 1, 2] AS i WITH DISTINCT i WHERE reduce(s = 0, x IN [1] | s + count(*)) > 0 RETURN i",
            _in_where("count(*)"),
        ),
        (
            "MATCH (p)-[:ACTED_IN]->(m) WITH p, count(*) AS c "
            "WHERE size([(p)-[:ACTED_IN]->(x) WHERE count(x) > 1 | x]) > 0 RETURN p.name",
            _in_where("count()"),
        ),
        (
            "MATCH (p)-[:ACTED_IN]->(m) WITH p, count(*) AS c WHERE EXISTS { (p)-[:KNOWS]->(q) WHERE count(q) > 0 } "
            "RETURN p.name",
            _in_where("count()"),
        ),
        (
            "UNWIND [1, 2] AS i CALL (i) { UNWIND [i, i] AS x WITH x WHERE count(*) > 1 RETURN x } RETURN x",
            _in_where("count(*)"),
        ),
        # #313, drivine4j's #497: there a variable from before the WITH is read only inside an aggregate, or where it is
        # a key, written as its item is.
        ("UNWIND [1, 2, 3] AS i WITH i % 2 AS k, count(*) AS c WHERE i > 0 RETURN k, c", _undefined("i")),
        (
            "UNWIND [1, 2, 3] AS i WITH i % 2 AS k, count(*) AS c WHERE sum(i) > 2 AND i > 0 RETURN k, c",
            _undefined("i"),
        ),
        ("UNWIND [1, 2, 3] AS i WITH DISTINCT i % 2 AS k WHERE i > 0 RETURN k", _undefined("i")),
        ("MATCH (p)-[:ACTED_IN]->(m) WITH p.name AS n, count(*) AS c WHERE p.born > 1965 RETURN n, c", _undefined("p")),
        ("MATCH (p)-[:ACTED_IN]->(m) WITH p, count(*) AS c WHERE m.released > 1996 RETURN p.name", _undefined("m")),
        ("UNWIND [1, 1, 2] AS i WITH i, count(*) AS c WHERE sum(c) > 1 RETURN i, c", _undefined("c")),
        ("UNWIND [1, 1, 2] AS i WITH i AS j, count(*) AS c WHERE sum(j) > 1 RETURN j, c", _undefined("j")),
        (
            "UNWIND [1, 1, 2] AS i WITH i AS j, count(*) AS c WHERE any(x IN [1] WHERE x = i) RETURN j, c",
            _undefined("i"),
        ),
    ],
)
def test_an_aggregate_in_a_where_is_refused_as_neo4j_refuses_it(
    compiler: type[SqlCypherCompiler], cypher: str, message: str
) -> None:
    # drivine4j's #489: Neo4j's 42I24, "invalid use of aggregate function". Postgres failed with an error of its own,
    # and Oracle gave rows. The spec's "Aggregate in a WHERE" holds most of these; the rest are drivine4j's.
    with pytest.raises(DrivineError) as refused:
        compiler("g").compile(cypher)
    assert str(refused.value) == message


@pytest.mark.parametrize("compiler", COMPILERS.values(), ids=COMPILERS.keys())
@pytest.mark.parametrize(
    "cypher",
    [
        "UNWIND [1, 2] AS i WITH count(*) AS c WHERE c > 1 RETURN c",
        "UNWIND [1, 2] AS i WITH collect(i) AS l WHERE size(l) > 1 RETURN l",
        "MATCH (n) RETURN COUNT { MATCH (n)-->(m) WITH count(m) AS c WHERE c > 0 RETURN c } AS k",
        "MATCH (n) WHERE EXISTS { MATCH (n)-->(m) WITH count(m) AS c WHERE c > 0 RETURN c } RETURN n.x",
        "MATCH (n) CALL (n) { MATCH (n)-->(m) WITH count(m) AS c WHERE c > 0 RETURN c } RETURN n.x, c",
        "MATCH (n) WITH n, count { (n)-->() } AS c WHERE c > 0 RETURN n.x",
        # #313, drivine4j's #497: the WHERE of a WITH that aggregates, or is DISTINCT, filters its groups, an aggregate
        # in it among them.
        "UNWIND [1, 1, 2] AS i WITH i, count(*) AS c WHERE count(*) > 1 RETURN i",
        "UNWIND [1, 1, 2] AS i WITH i, count(*) AS c WHERE sum(i) > 1 RETURN i",
        "UNWIND [1, 2] AS i WITH i AS j, count(*) AS c WHERE j > count(*) RETURN j",
        "UNWIND [1, 1, 2] AS i WITH DISTINCT i WHERE count(*) > 1 RETURN i",
        "UNWIND [1, 1, 2] AS i WITH i AS j, count(*) AS c WHERE i > 1 RETURN j, c",
        "UNWIND [1, 1, 2] AS i WITH i, count(*) AS c WHERE all(x IN collect(i) WHERE x > 1) RETURN i, c",
        "MATCH (n) WITH n, count(*) AS c "
        "WHERE EXISTS { MATCH (n)-->(m) WITH m, count(*) AS k WHERE count(*) > 0 RETURN m } RETURN n.x",
        "UNWIND [1, 2] AS i CALL (i) { UNWIND [i, i] AS x WITH x, count(*) AS c WHERE count(*) > 1 RETURN c } "
        "RETURN i, c",
    ],
)
def test_a_where_over_what_an_aggregate_gave_or_in_a_query_that_aggregates_in_its_own_projection_is_compiled(
    compiler: type[SqlCypherCompiler], cypher: str
) -> None:
    # Beside drivine4j's #489, as Neo4j runs them.
    compiler("g").compile(cypher)


def _ungrouped_item(name: str, variable: str) -> str:
    return (
        f"{name} aggregates, and names {variable} outside its aggregates, which no other item is: Cypher would group "
        f"by it without being told. Aggregate in a WITH first, as in WITH {variable}, collect(…) AS items "
        "RETURN { … }."
    )


def _ungrouped_term(variable: str) -> str:
    return (
        f"A term of an ORDER BY aggregates, and names {variable} outside its aggregates and the items: Cypher would "
        "group by it without being told. Order by an item's name, or by an expression of the items and aggregates."
    )


@pytest.mark.parametrize("compiler", COMPILERS.values(), ids=COMPILERS.keys())
@pytest.mark.parametrize(
    ("cypher", "message"),
    [
        ("UNWIND [1, 2] AS x UNWIND [3] AS z RETURN x, [y IN collect(z) | y + z] AS l", _ungrouped_item("l", "z")),
        (
            "UNWIND [1, 2] AS x UNWIND [3] AS z WITH x, [y IN collect(z) | y + z] AS l RETURN x, l",
            _ungrouped_item("l", "z"),
        ),
        (
            "UNWIND [1, 2] AS x UNWIND [3] AS z RETURN x, [y IN collect(x) WHERE y > z | y] AS l",
            _ungrouped_item("l", "z"),
        ),
        (
            "UNWIND [1, 2] AS x UNWIND [3] AS z RETURN x, any(y IN collect(x) WHERE y > z) AS l",
            _ungrouped_item("l", "z"),
        ),
        (
            "UNWIND [1, 2] AS x UNWIND [3] AS z RETURN x, reduce(s = 0, y IN collect(x) | s + z) AS l",
            _ungrouped_item("l", "z"),
        ),
        ("UNWIND [{a: 1, b: 2}] AS x RETURN x.a, [y IN collect(x) | y.a + x.b] AS l", _ungrouped_item("l", "x")),
        ("MATCH (n) RETURN size([(n)-->(m) | m]) + count(*) AS c", _ungrouped_item("c", "n")),
        ("MATCH (n) RETURN n.x, size([(n)-->(m) | m.x]) + count(*) AS c", _ungrouped_item("c", "n")),
        ("MATCH (n) RETURN EXISTS { (n)-->() } AND count(*) > 0 AS b", _ungrouped_item("b", "n")),
        ("UNWIND [{a: 1}, {a: 2}] AS x RETURN x.a AS a, count(*) AS c ORDER BY x.b + count(*)", _ungrouped_term("x")),
        (
            "UNWIND [{a: 1, b: 2}] AS x WITH x.a AS a, count(*) AS c ORDER BY x.b + count(*) RETURN a",
            _ungrouped_term("x"),
        ),
        ("UNWIND [1, 2] AS x UNWIND [3] AS z RETURN x, count(*) AS c ORDER BY z + count(*)", _ungrouped_term("z")),
        (
            "UNWIND [1, 2] AS x UNWIND [3] AS z RETURN x, count(*) AS c ORDER BY [w IN collect(z) | w + z]",
            _ungrouped_term("z"),
        ),
        (
            "UNWIND [1, 2] AS x UNWIND [3] AS z RETURN x, count(*) AS c ORDER BY reduce(s = 0, w IN collect(x) | z)",
            _ungrouped_term("z"),
        ),
        (
            "UNWIND [1, 2] AS x UNWIND [3] AS z RETURN x, count(*) AS c ORDER BY any(w IN collect(x) WHERE w > z)",
            _ungrouped_term("z"),
        ),
        (
            "MATCH (me:Person)--(you:Person) RETURN count(you.age) AS agg ORDER BY me.age + count(you.age)",
            _ungrouped_term("me"),
        ),
        (
            "MATCH (p:Person)-[:ACTED_IN]->(m) RETURN p AS q, count(*) AS c ORDER BY size(keys(m {.title})) + count(*)",
            _ungrouped_term("m"),
        ),
        # Neo4j runs these, but reads p in the pattern as a new variable that matches any node, not as the key q is.
        (
            "MATCH (p:Person)-[:ACTED_IN]->(m) RETURN p AS q, count(*) AS c "
            "ORDER BY size([(p)-[:KNOWS]->(k) | k]) + count(*)",
            _ungrouped_term("p"),
        ),
        (
            "MATCH (p:Person)-[:ACTED_IN]->(m) RETURN p AS q, count(*) AS c "
            "ORDER BY EXISTS { (p)-[:KNOWS]->() } AND count(*) > 1",
            _ungrouped_term("p"),
        ),
    ],
)
def test_an_aggregating_item_or_order_term_that_names_a_variable_no_item_groups_by_in_a_body_too_is_refused(
    compiler: type[SqlCypherCompiler], cypher: str, message: str
) -> None:
    # drivine4j's #482: Neo4j's 42I18, "reference to non-grouping sub-expression". The body of a comprehension, a
    # quantifier, a reduce() or a pattern was not read for the variables it names, nor an order term at all. The spec's
    # "Implicit grouping key in a body or an ORDER BY" holds most of these; the rest are drivine4j's.
    with pytest.raises(DrivineError) as refused:
        compiler("g").compile(cypher)
    assert str(refused.value) == message


@pytest.mark.parametrize("compiler", COMPILERS.values(), ids=COMPILERS.keys())
@pytest.mark.parametrize(
    "cypher",
    [
        "UNWIND [1, 2] AS x RETURN x, [y IN collect(x) | y + x] AS l",
        "UNWIND [1, 2] AS x RETURN x, collect(x) AS c",
        "UNWIND [1, 2] AS x RETURN [x IN collect(x) | x + 1] AS l",
        "UNWIND [1, 2] AS x RETURN x, reduce(s = 0, y IN collect(x) | s + y + x) AS l",
        "UNWIND [{a: 1}, {a: 2}] AS x RETURN x.a, count(*) AS c",
        "UNWIND [{a: 1}] AS x RETURN x.a, [y IN collect(x) | y.a + x.a] AS l",
        "MATCH (n) RETURN n, size([(n)-->(m) | m]) + count(*) AS c",
        "MATCH (n) RETURN n, EXISTS { (n)-->() } AND count(*) > 0 AS b",
        "UNWIND [{a: 1}, {a: 2}] AS x RETURN x.a AS a, count(*) AS c ORDER BY x.a + count(*)",
        "UNWIND [1, 2] AS x RETURN x AS y, count(*) AS c ORDER BY x + count(*)",
        "UNWIND [1, 2] AS x RETURN x AS y, count(*) AS c ORDER BY y + count(*)",
        "UNWIND [{a: 1, b: 2}] AS x RETURN x.a + x.b, count(*) AS cnt ORDER BY x.a + x.b + count(*)",
        "UNWIND [1, 2] AS x UNWIND [3] AS z RETURN x, count(*) AS c ORDER BY [w IN collect(z) | w + x]",
        # A key an item renames, named by an order term's map projection, or by a pattern where no item renames it.
        "MATCH (p:Person)-[:ACTED_IN]->(m) RETURN p AS q, count(*) AS c ORDER BY size(keys(p {.name})) + count(*)",
        "MATCH (p:Person)-[:ACTED_IN]->(m) RETURN p AS q, count(*) AS c ORDER BY p {.born}.born + count(*)",
        "MATCH (p:Person)-[:ACTED_IN]->(m) RETURN p, count(*) AS c ORDER BY size([(p)-[:KNOWS]->(k) | k]) + count(*)",
        # A key named in another case, or spaced and bracketed otherwise: Neo4j names a function in any case.
        "UNWIND [{a: 'x'}] AS x RETURN toUpper(x.a) AS u, count(*) AS c ORDER BY TOUPPER(x.a) + count(*)",
        "UNWIND [{a: 'x'}] AS x RETURN toUpper( x.a ) AS u, count(*) AS c ORDER BY (toUpper(x.a)) + count(*)",
    ],
)
def test_a_body_or_order_term_that_names_only_what_the_projection_groups_by_is_compiled(
    compiler: type[SqlCypherCompiler], cypher: str
) -> None:
    # Beside drivine4j's #482: a body or an order term that names only a grouping key, an item's name, or a variable of
    # its own, as Neo4j runs it.
    compiler("g").compile(cypher)


@pytest.mark.parametrize("compiler", COMPILERS.values(), ids=COMPILERS.keys())
@pytest.mark.parametrize(
    ("cypher", "message"),
    [
        ("UNWIND [{a: 1, b: 2}] AS x RETURN x.a AS a, x.b + count(*) AS c", _ungrouped_item("c", "x")),
        (
            "MATCH (p:Person)-[:ACTED_IN]->(m) RETURN toUpper(p.name) AS u, toUpper(p.name) + count(*) AS c ORDER BY u",
            _ungrouped_item("c", "p"),
        ),
        (
            "MATCH (p:Person)-[:ACTED_IN]->(m) RETURN toUpper(p.name) AS u, "
            "[t IN collect(m.title) | t + toUpper(p.name)] AS c ORDER BY u",
            _ungrouped_item("c", "p"),
        ),
        (
            "UNWIND [{a: {b: 1}}, {a: {b: 1}}, {a: {b: 2}}] AS x RETURN x.a.b AS k, x.a.b + count(*) AS c ORDER BY k",
            _ungrouped_item("c", "x"),
        ),
        (
            "UNWIND [{a: {b: 1}}, {a: {b: 1}}, {a: {b: 2}}] AS x RETURN x.a.b AS k, "
            "[y IN collect(x) | y.a.b + x.a.b] AS c ORDER BY k",
            _ungrouped_item("c", "x"),
        ),
        (
            "UNWIND [{a: 1}, {a: 1}, {a: 2}] AS x RETURN x.a + 1 AS k, x.a + 1 + count(*) AS c ORDER BY k",
            _ungrouped_item("c", "x"),
        ),
        (
            "UNWIND [{a: 1}, {a: 1}, {a: 2}] AS x RETURN x['a'] AS a, x['a'] + count(*) AS b ORDER BY a",
            _ungrouped_item("b", "x"),
        ),
        (
            "MATCH (p:Person)-[:ACTED_IN]->(m) RETURN p.name AS name, "
            "size([(q:Person)-[:KNOWS]->() WHERE q.name = p.name | q]) + count(*) AS c ORDER BY name",
            _ungrouped_item("c", "p"),
        ),
        (
            "MATCH (p:Person)-[:ACTED_IN]->(m) RETURN p.name AS name, "
            "[x IN collect(m) | size([(x)<-[:ACTED_IN]-(q) WHERE q.name = p.name | q])] AS c ORDER BY name",
            _ungrouped_item("c", "p"),
        ),
        # Unnamed, the map projection is the item p, not the variable.
        (
            "MATCH (p:Person)-[:ACTED_IN]->(m) RETURN p {.name}, count(*) + size(p.name) AS c ORDER BY p.name",
            _ungrouped_item("c", "p"),
        ),
        (
            "MATCH (p:Person)-[:ACTED_IN]->(m) RETURN p {.name} AS q, p {.name}.name + count(*) AS c",
            _ungrouped_item("c", "p"),
        ),
    ],
)
def test_an_aggregating_item_that_reads_a_key_neither_a_variable_nor_a_variables_property_is_refused(
    compiler: type[SqlCypherCompiler], cypher: str, message: str
) -> None:
    # #311, drivine4j's #491: Neo4j reads `x.a` in `x.a + count(*)` as the key `x.a`, but no key of another shape there,
    # beside the item's aggregates or in a body (its 42I18), and in a pattern's parts only a key that is a variable.
    # drivine4j's CompilerRefusalTest; the spec's "Implicit grouping key in a body or an ORDER BY" holds most of these.
    with pytest.raises(DrivineError) as refused:
        compiler("g").compile(cypher)
    assert str(refused.value) == message


@pytest.mark.parametrize("compiler", COMPILERS.values(), ids=COMPILERS.keys())
@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (x) RETURN x.a AS a, x.a + count(*) AS b",
        "UNWIND [{a: 1}, {a: 1}, {a: 2}] AS x RETURN x.a, x.a + count(*) AS b ORDER BY x.a",
        "UNWIND [{a: 1}, {a: 1}, {a: 2}] AS x RETURN x.a + count(*) AS b, x.a AS a ORDER BY a",
        "UNWIND [{a: 1}, {a: 1}, {a: 2}] AS x RETURN x.a AS a, count(*) + x.a AS b, max(x.a) - x.a AS c ORDER BY a",
        "MATCH (:Person)-[r:ACTED_IN]->(m) RETURN r.role AS role, "
        "CASE WHEN r.role STARTS WITH 'N' THEN count(*) ELSE 0 END AS n ORDER BY role",
        "MATCH (p:Person)-[:ACTED_IN]->(m) RETURN p.name, reduce(s = p.name, t IN collect(m.title) | s + t) AS r "
        "ORDER BY p.name",
        "MATCH (p:Person)-[:ACTED_IN]->(m) RETURN p.name, p.name IN collect(m.title) AS r ORDER BY p.name",
        "UNWIND [{a: {b: 1}}, {a: {b: 1}}, {a: {b: 2}}] AS x RETURN x.a AS k, [y IN collect(x) | x.a.b] AS c "
        "ORDER BY c",
        "MATCH (p:Person)-[:ACTED_IN]->(m) RETURN p AS q, p.name + count(*) AS c ORDER BY c",
        "MATCH (p:Person)-[:ACTED_IN]->(m) RETURN p {.name}, collect(m.title) AS ts ORDER BY p.name",
        "MATCH (p:Person)-[:ACTED_IN]->(m) WITH p {.name}, count(*) AS c RETURN p.name, c ORDER BY p.name",
        "UNWIND [{a: 'x'}, {a: 'x'}, {a: 'y'}] AS x RETURN toUpper(x.a), count(*) ORDER BY TOUPPER(x.a) + count(*)",
        "MATCH (p:Person)-[:ACTED_IN]->(m) RETURN toUpper(p.name) AS u, COUNT(m) AS c "
        "ORDER BY toUpper(p.name) + count(m) DESC",
    ],
)
def test_an_aggregating_item_that_reads_a_key_that_is_a_variables_property_is_compiled(
    compiler: type[SqlCypherCompiler], cypher: str
) -> None:
    # #311, drivine4j's #491: as Neo4j runs them. drivine4j's CompilerRefusalTest.
    compiler("g").compile(cypher)
