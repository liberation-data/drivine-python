"""
A statement the compiler cannot make SQL of, or that is wrong as Cypher is, is refused when it is compiled, with what is
wrong, on each store alike: before the store is sent anything. The statements and messages are the Cypher spec's, in
tests/cypher/refusals.yaml.
"""

from typing import cast

import pytest

from drivine.cypher.shared_compiler import SharedStore, compile_cypher
from tests.cypher.cypher_spec import HELD_FAILURES, Case, expanded, failure_matches, held_for, held_passes, load

SPEC = load("refusals.yaml")


@pytest.mark.parametrize("store", ["postgres", "oracle", "sqlite"])
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
    """That the step at `index` of the case is refused on the store, as it says, by the shared compiler Drivine runs."""
    step = case.steps[index]
    where = f"{case.name}, step {index + 1}, on {store}: {step.cypher.strip()}"
    with pytest.raises(Exception) as refused:  # noqa: PT011
        compile_cypher(step.cypher, expanded(step.parameters), cast(SharedStore, store.upper()), "g")
    assert failure_matches(step, refused.value), f"{where}\nexpected: {step.error}\nactual:   {refused.value}"
