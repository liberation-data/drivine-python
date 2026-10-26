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
        ("RETURN round(1234.5, -1.2)", {}),
        ("RETURN round(1234.5, -$p)", {"p": 2}),
        ("RETURN round(1234.5, $p)", {"p": -2.5}),
        ("RETURN round(1234.5, $p)", {"p": Decimal("-1.2")}),
        ("RETURN round(1234.5, $p)", {"p": float("-inf")}),
    ],
)
def test_round_of_two_arguments_refuses_a_precision_below_0_toward_zero_written_negated_or_given(
    compiler: type[SqlCypherCompiler], cypher: str, parameters: dict[str, Any]
) -> None:
    # Not in the spec's refusals yet, which name the message for three arguments alone.
    with pytest.raises(DrivineError, match=r"^round\(\) takes a precision of 0 or more, as in Neo4j\.$"):
        compiler("g").compile(cypher, parameters)


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
@pytest.mark.parametrize("p", [-0.9, Decimal("-0.5"), float("nan")])
def test_round_takes_a_precision_above_minus_1_as_0_places_and_compiles_one_that_is_nan(
    compiler: type[SqlCypherCompiler], p: Any
) -> None:
    # -0.5 and -0.9 cut toward zero are 0 places; NaN is no precision below 0, and is left to the store.
    assert compiler("g").compile("RETURN [round(1234.5, -0.5), round(1234.5, $p, 'HALF_UP')]", {"p": p}).sql
