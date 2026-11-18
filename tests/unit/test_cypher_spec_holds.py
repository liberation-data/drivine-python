"""
The spec runner's holds (tests/cypher/cypher_spec.py `HELD`): a held step must fail unless it may pass, and a skipped
one isn't run.
"""

import pytest

from tests.cypher import cypher_spec
from tests.cypher.cypher_spec import Hold, held_for, load, may_pass, skipped_for

QUERIES = load("queries.yaml")


def test_collect_skips_nulls_is_skipped_on_oracle_alone_and_held_on_no_store():
    # Oracle's server sometimes ends the session in this case, which passes in most runs (drivine4j#838).
    case = next(it for it in QUERIES.cases if it.name == "collect skips nulls")
    assert skipped_for("queries.yaml", case, 0, "oracle") == "drivine4j#838"
    for store in ("postgres", "sqlite", "neo4j"):
        assert skipped_for("queries.yaml", case, 0, store) is None
    for store in ("oracle", "postgres", "sqlite", "neo4j"):
        assert held_for("queries.yaml", case, 0, store) is None


def test_a_skip_on_some_steps_skips_those_steps_alone(monkeypatch: pytest.MonkeyPatch):
    case = next(it for it in QUERIES.cases if it.name == "collect skips nulls")
    hold = Hold(1, ("sql",), steps=(2, 4), skip=True)
    monkeypatch.setattr(cypher_spec, "HELD", {"queries.yaml": {case.name: [hold]}})
    assert [skipped_for("queries.yaml", case, index, "oracle") for index in range(4)] == [
        None,
        "drivine-python#1",
        None,
        "drivine-python#1",
    ]
    assert [skipped_for("queries.yaml", case, index, "neo4j") for index in range(4)] == [None] * 4
    assert [held_for("queries.yaml", case, index, "oracle") for index in range(4)] == [None] * 4


def test_a_hold_that_isnt_a_skip_skips_no_step():
    case = next(it for it in QUERIES.cases if it.name == "A regular expression with a backreference")
    assert held_for("queries.yaml", case, 0, "sqlite") == "drivine-cypher-spec#240"
    assert [skipped_for("queries.yaml", case, index, "sqlite") for index in range(len(case.steps))] == [None] * len(
        case.steps
    )


def test_the_dropped_p_where_case_is_held_on_neo4j_at_steps_11_and_110_and_may_pass_there():
    # CI's Neo4j gives four rows where the box's gives none (drivine#764): held so either answer passes.
    name = "Nesting of a dropped variable: a WITH that drops p and groups, in its WHERE"
    case = next(it for it in QUERIES.cases if it.name == name)
    held = [index for index in range(len(case.steps)) if held_for("queries.yaml", case, index, "neo4j")]
    assert held == [10, 109]
    assert all(held_for("queries.yaml", case, index, "neo4j") == "drivine#764" for index in held)
    assert all(may_pass("queries.yaml", case, index, "neo4j") for index in held)
    for store in ("memgraph", "falkordb", "postgres", "sqlite", "oracle"):
        assert [held_for("queries.yaml", case, index, store) for index in range(len(case.steps))] == [None] * len(
            case.steps
        )
    assert [skipped_for("queries.yaml", case, index, "neo4j") for index in range(len(case.steps))] == [None] * len(
        case.steps
    )


def test_a_hold_that_may_not_pass_must_fail():
    case = next(it for it in QUERIES.cases if it.name == "A regular expression with a backreference")
    assert held_for("queries.yaml", case, 0, "sqlite") == "drivine-cypher-spec#240"
    assert not may_pass("queries.yaml", case, 0, "sqlite")
