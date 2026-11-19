"""
A `time` with an offset saved and loaded back as that time, in a field and in a list, and bound as a parameter, on each
store (#565, as liberation-data/drivine4j#862). The Neo4j driver couldn't pack one whose zone is the standard
library's `timezone`, so on Neo4j and Memgraph a save or a bind of it failed. Neo4j now holds it as a Time of its
offset; Memgraph, which has no Time, holds it as text of one width, as drivine4j writes it, and FalkorDB its ISO text,
either of which a field declared `time` reads back as the time. A naive time binds, saves and loads as before.
"""

from datetime import time, timedelta, timezone
from typing import Annotated, Any

import pytest

from drivine import (
    DatabaseType,
    NodeFragment,
    NodeId,
    PersistenceManager,
    QuerySpecification,
    StatelessGraphObjectManager,
)

PLUS_TWO = timezone(timedelta(hours=2))
STARTS = time(10, 11, 12, tzinfo=PLUS_TWO)
ENDS = time(18, 30, 0, 250000, tzinfo=timezone(timedelta(hours=-5, minutes=-30)))
NAIVE = time(9, 0, 0)


class Shift(NodeFragment, labels=["OffsetShift"]):
    id: Annotated[str, NodeId()]
    starts: time | None = None
    naive: time | None = None
    times: list[time] = []


def assert_same_time(loaded: Any, given: time) -> None:
    """`loaded` is `given`: a `time` of the same clock reading and the same offset, or none where it has none."""
    assert isinstance(loaded, time)
    assert loaded.replace(tzinfo=None) == given.replace(tzinfo=None)
    assert loaded.utcoffset() == given.utcoffset()


async def test_a_time_with_an_offset_saves_and_loads_back_as_that_time(pm: PersistenceManager):
    gom = StatelessGraphObjectManager(pm)
    await gom.save(Shift(id="s1", starts=STARTS, naive=NAIVE, times=[STARTS, ENDS]))
    loaded = await gom.load(Shift, "s1")
    assert loaded is not None
    assert_same_time(loaded.starts, STARTS)
    assert_same_time(loaded.naive, NAIVE)
    assert len(loaded.times) == 2
    assert_same_time(loaded.times[0], STARTS)
    assert_same_time(loaded.times[1], ENDS)


async def test_a_time_with_an_offset_saved_again_from_its_loaded_copy_loads_back_as_that_time(pm: PersistenceManager):
    gom = StatelessGraphObjectManager(pm)
    await gom.save(Shift(id="s2", starts=STARTS))
    loaded = await gom.load(Shift, "s2")
    assert loaded is not None
    await gom.save(loaded.model_copy(update={"times": [loaded.starts]}))
    again = await gom.load(Shift, "s2")
    assert again is not None
    assert_same_time(again.starts, STARTS)
    assert_same_time(again.times[0], STARTS)


STORED_AS_TEXT = {
    DatabaseType.MEMGRAPH: ("10:11:12.000000000+02:00", "18:30:00.250000000-05:30"),
    DatabaseType.FALKORDB: ("10:11:12+02:00", "18:30:00.250000-05:30"),
}
"""
The engines with no Time, and the text each holds STARTS and ENDS as: Memgraph's of one width, as drivine4j writes it,
FalkorDB's the ISO text its parameter coercer writes.
"""


@pytest.mark.graph_engine_only("a statement of the graph engines' own, sent as it is")
async def test_a_time_with_an_offset_binds_as_a_parameter(pm: PersistenceManager):
    rows = await pm.query(QuerySpecification[Any]("RETURN $t AS t").bind({"t": STARTS}))
    if pm.type in STORED_AS_TEXT:
        assert rows == [STORED_AS_TEXT[pm.type][0]]
    else:
        assert_same_time(rows[0], STARTS)


@pytest.mark.graph_engine_only("a statement of the graph engines' own, sent as it is")
async def test_a_time_with_an_offset_binds_in_a_list_as_a_parameter(pm: PersistenceManager):
    rows = await pm.query(QuerySpecification[Any]("RETURN $ts AS ts").bind({"ts": [STARTS, ENDS]}))
    if pm.type in STORED_AS_TEXT:
        assert rows == [list(STORED_AS_TEXT[pm.type])]
    else:
        assert_same_time(rows[0][0], STARTS)
        assert_same_time(rows[0][1], ENDS)


@pytest.mark.graph_engine_only("a statement of the graph engines' own, sent as it is")
async def test_a_bound_time_with_an_offset_finds_the_node_saved_with_it(pm: PersistenceManager):
    await StatelessGraphObjectManager(pm).save(Shift(id="s3", starts=STARTS))
    rows = await pm.query(
        QuerySpecification[Any]("MATCH (s:OffsetShift) WHERE s.starts = $t RETURN s.id").bind({"t": STARTS})
    )
    assert rows == ["s3"]


@pytest.mark.graph_engine_only("a statement of the graph engines' own, sent as it is")
async def test_a_naive_time_binds_as_before(pm: PersistenceManager):
    rows = await pm.query(QuerySpecification[Any]("RETURN $t AS t").bind({"t": NAIVE}))
    if pm.type is DatabaseType.FALKORDB:
        assert rows == ["09:00:00"]
    else:
        assert rows == [NAIVE]
