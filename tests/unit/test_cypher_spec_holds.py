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


def test_a_hold_that_may_pass_lets_its_steps_pass_on_its_stores_alone(monkeypatch: pytest.MonkeyPatch):
    case = next(it for it in QUERIES.cases if it.name == "collect skips nulls")
    hold = Hold(1, ("neo4j",), steps=(2,), may_pass="it passes on one host and fails on another")
    monkeypatch.setattr(cypher_spec, "HELD", {"queries.yaml": {case.name: [hold]}})
    assert [held_for("queries.yaml", case, index, "neo4j") for index in range(3)] == [None, "drivine-python#1", None]
    assert [may_pass("queries.yaml", case, index, "neo4j") for index in range(3)] == [False, True, False]
    assert [skipped_for("queries.yaml", case, index, "neo4j") for index in range(3)] == [None] * 3
    for store in ("postgres", "sqlite", "oracle"):
        assert [may_pass("queries.yaml", case, index, store) for index in range(3)] == [False] * 3


def test_the_dropped_p_where_case_holds_no_step_as_the_spec_pins_no_neo4j_answer_for_it():
    # The spec no longer pins Neo4j's answer for a dropped p read in the WITH's WHERE through a list comprehension,
    # which it gave differently by host (liberation-data/drivine-cypher-spec#366, #368; drivine#764), so the two
    # steps that were held for it are no steps of the case.
    name = "Nesting of a dropped variable: a WITH that drops p and groups, in its WHERE"
    case = next(it for it in QUERIES.cases if it.name == name)
    assert not any(" WHERE p.name IS NOT NULL]" in it.cypher for it in case.steps)
    assert name not in cypher_spec.HELD["queries.yaml"]


def test_a_hold_that_may_not_pass_must_fail():
    case = next(it for it in QUERIES.cases if it.name == "A regular expression with a backreference")
    assert held_for("queries.yaml", case, 0, "sqlite") == "drivine-cypher-spec#240"
    assert not may_pass("queries.yaml", case, 0, "sqlite")
