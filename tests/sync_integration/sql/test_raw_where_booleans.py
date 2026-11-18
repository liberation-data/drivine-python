# Written by scripts/unasync.py from tests/integration/sql/test_raw_where_booleans.py:
# change that, and run the script again.
"""
A raw where in Cypher's shorthand that compares a property with a parameter bound to a boolean, on each SQL store
(drivine-python#542, liberation-data/drivine4j#849). The shorthand reads a property as text, `'true'` for a JSON true,
and a driver binds a boolean as no such text: SQLite's as 1, so `n.flag = $flag` found nothing in a load, a count, a
delete, a patch or a grouped read. It is compared with the boolean's text, as `n.flag = true` is.
"""

from collections.abc import Iterator
from typing import Annotated

import pytest

from drivine.sync import NodeFragment, NodeId, PersistenceManager, StatelessGraphObjectManager, count, key
from tests.sync_integration.sql.sql_seed import SqlSeed


class Flagged(NodeFragment, labels=["RawFlagged"]):
    id: Annotated[str, NodeId()]
    flag: bool = False
    note: str = ""


@pytest.fixture(autouse=True)
def flagged(pm: PersistenceManager, gom: StatelessGraphObjectManager) -> Iterator[None]:
    seed = SqlSeed(pm, "RawFlagged")
    seed.clear()
    gom.save_all([Flagged(id="b1", flag=True), Flagged(id="b2", flag=False)])
    yield
    seed.clear()


WHERE = "n.flag = $flag"


def test_a_load_and_a_count_find_the_nodes_that_hold_the_bound_boolean(gom: StatelessGraphObjectManager):
    assert [it.id for it in gom.load_all(Flagged, WHERE, {"flag": True})] == ["b1"]
    assert [it.id for it in gom.load_all(Flagged, "n.flag <> $flag", {"flag": True})] == ["b2"]
    assert [it.id for it in gom.load_all(Flagged, "n.flag != $flag", {"flag": False})] == ["b1"]
    assert gom.count(Flagged, WHERE, {"flag": False}) == 1


def test_a_patch_meets_the_node_that_holds_it_and_not_one_that_doesnt(gom: StatelessGraphObjectManager):
    assert (gom.patch(Flagged, "b1", set={"note": "met"}, where=WHERE, bindings={"flag": True})).applied
    assert not (gom.patch(Flagged, "b1", set={"note": "missed"}, where=WHERE, bindings={"flag": False})).applied
    loaded = gom.load(Flagged, "b1")
    assert loaded is not None and loaded.note == "met"


def test_a_grouped_reads_where_and_aggregate_filter_read_it_so_too(gom: StatelessGraphObjectManager):
    keyed = {"id": key("id")}
    assert gom.groups.load(Flagged, where=WHERE, bindings={"flag": True}, keys=keyed) == [{"id": "b1"}]
    counted = gom.groups.load(
        Flagged, keys=keyed, aggregates={"flagged": count(WHERE)}, bindings={"flag": True}, order_by=[("id", "asc")]
    )
    assert counted == [{"id": "b1", "flagged": 1}, {"id": "b2", "flagged": 0}]


def test_a_delete_takes_the_nodes_that_hold_it(gom: StatelessGraphObjectManager):
    assert gom.delete_all(Flagged, WHERE, {"flag": False}) == 1
    assert [it.id for it in gom.load_all(Flagged)] == ["b1"]
