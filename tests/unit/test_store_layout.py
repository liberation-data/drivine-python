"""
The store layout's verdict (store_layout): marker.yaml's storeLayout cases, each asked of the shared module as a first
connection asks it, and what the verdict has a first connection run, a statement at a time, as `judging` yields them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from drivine import StoreLayoutError
from drivine.backend.sql.store_layout import Ask, judge, judging, layout_verdict
from drivine.backend.store_statements import store_statements

CASES: list[dict[str, Any]] = yaml.safe_load((Path(__file__).parents[1] / "cypher" / "marker.yaml").read_text())[
    "storeLayout"
]["cases"]


def expected(verdict: Any) -> dict[str, Any]:
    """A case's verdict as the module answers it: `"current"`, `{stamp: {drops, assign?}}` or `{refuse}`."""
    if verdict == "current":
        return {"verdict": "current"}
    if "refuse" in verdict:
        return {"verdict": "refuse", "message": verdict["refuse"]}
    return {"verdict": "stamp", "drops": verdict["stamp"]["drops"], "assign": verdict["stamp"].get("assign", False)}


@pytest.mark.parametrize("case", CASES, ids=[it["name"] for it in CASES])
def test_each_cases_verdict_is_the_modules(case: dict[str, Any]):
    evidence = {"stamps": case["stamps"], "foreignNodes": case["foreignNodes"], "objects": case.get("objects", [])}
    answer = layout_verdict(case["store"].upper(), "drivine", evidence)
    found = {key: answer[key] for key in expected(case["verdict"])}
    assert found == expected(case["verdict"]), case["name"]


class Store:
    """What a SQLite graph's statements give, by which statement of the module's it is: the rest give no row."""

    def __init__(self, *answers: tuple[str, list[Any]]) -> None:
        self.answers = list(answers)
        self.asked: list[Ask] = []

    def rows(self, statement: str, parameters: Any) -> list[Any]:
        self.asked.append(Ask(statement, parameters))
        for i, (it, rows) in enumerate(self.answers):
            if it == statement:
                del self.answers[i]
                return rows
        return []


STAMP = store_statements("sqlite", "g").stamp
MADE = "SELECT 1 /* the graph's table of nodes */"


def stamped(layout: int | None) -> list[Any]:
    return [{"stamp": json.dumps({"id": "x", "assignedAt": "t", "layout": layout})}]


def test_a_new_graph_is_asked_nothing_but_whether_its_table_of_nodes_is_there():
    store = Store((MADE, [{"made": 0}]))
    judge("sqlite", "g", MADE, store.rows)
    assert [it.statement for it in store.asked] == [MADE]


def test_an_empty_graph_of_an_earlier_layout_is_restamped_and_judged_again():
    store = Store(
        (MADE, [{"made": 1}]),
        (STAMP.read_stamps, stamped(1)),
        (STAMP.read_stamps, stamped(2)),
    )
    judge("sqlite", "g", MADE, store.rows)
    statements = [it.statement for it in store.asked]
    assert STAMP.restamp_layout in statements
    restamp = next(it for it in store.asked if it.statement == STAMP.restamp_layout)
    assert dict(restamp.parameters) == {"key": "singleton", "layout": 2}
    # Read back after the restamp, and found this layout's.
    assert statements.count(STAMP.read_stamps) == 2


def test_a_cleared_graph_of_this_layout_is_stamped_again_with_this_processs_id_and_moment():
    lock = [{"o": json.dumps({"name": "g_lock2", "type": "TABLE"})}]
    store = Store(
        (MADE, [{"made": 1}]),
        (STAMP.read_stamps, []),
        (STAMP.foreign_nodes, [{"one": 1}]),
        (STAMP.layout_objects, lock),
        (STAMP.read_stamps, stamped(2)),
        (STAMP.foreign_nodes, [{"one": 1}]),
        (STAMP.layout_objects, lock),
    )
    judge("sqlite", "g", MADE, store.rows)
    [assigned] = [it for it in store.asked if it.statement == STAMP.assign_if_absent]
    assert assigned.parameters["layout"] == 2 and assigned.parameters["engine"] == "SQLITE"
    assert len(assigned.parameters["storeId"]) == 36 and assigned.parameters["assignedAt"].endswith("Z")


def test_a_graph_whose_stamps_read_back_otherwise_after_its_restamp_is_refused():
    # Another process stamps it with an earlier layout again between the restamp and the read.
    store = Store((MADE, [{"made": 1}]), (STAMP.read_stamps, stamped(1)), (STAMP.read_stamps, stamped(1)))
    with pytest.raises(StoreLayoutError, match="its stamps read back otherwise") as refused:
        judge("sqlite", "g", MADE, store.rows)
    assert refused.value.graph == "g"


def test_a_graph_of_an_earlier_layout_holding_values_is_refused_by_the_modules_message():
    store = Store((MADE, [{"made": 1}]), (STAMP.read_stamps, stamped(1)), (STAMP.foreign_nodes, [{"one": 1}]))
    with pytest.raises(StoreLayoutError, match="was written in store layout 1") as refused:
        judge("sqlite", "g", MADE, store.rows)
    assert refused.value.graph == "g"
    assert STAMP.restamp_layout not in [it.statement for it in store.asked]


def test_judging_yields_each_statement_and_takes_its_rows():
    asking = judging("sqlite", "g", MADE)
    assert next(asking) == Ask(MADE)
    assert asking.send([{"made": 1}]).statement == STAMP.read_stamps
