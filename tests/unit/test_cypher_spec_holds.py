"""The spec runner's holds (tests/cypher/cypher_spec.py `HELD`): a held step must fail, and a skipped one isn't run."""

import pytest

from tests.cypher import cypher_spec
from tests.cypher.cypher_spec import Hold, held_for, load, skipped_for

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
