"""
A statement the compiler cannot make SQL of, or that is wrong as Cypher is, is refused when it is compiled, with what is
wrong, on each store alike: before the store is sent anything. The statements and messages are the Cypher spec's, in
tests/cypher/refusals.yaml.
"""

import pytest

from drivine import DrivineError
from drivine.cypher.sql.oracle_cypher_compiler import OracleCypherCompiler
from drivine.cypher.sql.postgres_cypher_compiler import PostgresCypherCompiler
from drivine.cypher.sql.sql_cypher_compiler import SqlCypherCompiler
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
