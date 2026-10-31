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
from tests.cypher.cypher_spec import Case, expanded, failure_matches, load

SPEC = load("refusals.yaml")

COMPILERS: dict[str, type[SqlCypherCompiler]] = {"postgres": PostgresCypherCompiler, "oracle": OracleCypherCompiler}


@pytest.mark.parametrize("store", COMPILERS)
@pytest.mark.parametrize("case", SPEC.cases, ids=[it.name for it in SPEC.cases])
def test_refusals(case: Case, store: str) -> None:
    for index, step in enumerate(case.steps):
        if not step.runs_on(store):
            continue
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
