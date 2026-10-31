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
