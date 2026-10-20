import logging
from typing import Annotated

import pytest

from drivine import (
    DrivineError,
    IndexAdvicePolicy,
    NodeFragment,
    NodeId,
    PersistenceManager,
    StatelessGraphObjectManager,
    range_index,
    refs,
)
from tests.integration.stores import create


class Ticket(NodeFragment, labels=["AdviceTest"]):
    id: Annotated[str, NodeId()]
    opened: int
    rank: int


BY_OPENED = range_index("AdviceTest", "opened")
BY_OPENED_AND_RANK = range_index("AdviceTest", ["opened", "rank"])


@pytest.fixture
async def gom(pm: PersistenceManager):
    gom = StatelessGraphObjectManager(pm)
    gom.index_advice = IndexAdvicePolicy.FAIL
    await gom.indexes.drop(BY_OPENED_AND_RANK)
    await gom.indexes.drop(BY_OPENED)
    await create(pm, "AdviceTest", {"id": "t1", "opened": 1, "rank": 2}, {"id": "t2", "opened": 2, "rank": 1})
    yield gom
    await gom.indexes.drop(BY_OPENED_AND_RANK)
    await gom.indexes.drop(BY_OPENED)


q = refs(Ticket)


async def test_refuses_an_unindexed_order_when_told_to_fail_naming_the_index_to_make(gom):
    with pytest.raises(
        DrivineError,
        match=r"order_by on AdviceTest\(opened\) has no matching range index"
        r".*range_index\('AdviceTest', \['opened'\]\)",
    ):
        await gom.load_all(Ticket, order_by=q.opened.asc())
    with pytest.raises(DrivineError, match=r"seek on AdviceTest\(opened\)"):
        await gom.load_all(Ticket, order_by=q.opened.asc(), seek=q.opened.after(1))


async def test_does_not_advise_an_order_of_one_root_pinned_by_its_id(gom):
    one = await gom.load_all(Ticket, where=q.id.eq("t1"), order_by=q.opened.asc())
    assert [it.id for it in one] == ["t1"]


async def test_takes_an_index_over_exactly_the_ordered_properties_once_it_is_seen(gom):
    with pytest.raises(DrivineError, match="no matching"):
        await gom.load_all(Ticket, order_by=q.opened.asc())
    await gom.indexes.ensure(BY_OPENED)
    with pytest.raises(DrivineError, match="no matching"):  # the list read before the index was made is kept
        await gom.load_all(Ticket, order_by=q.opened.asc())
    gom.index_advisor.refresh()
    rows = await gom.load_all(Ticket, order_by=q.opened.desc())
    assert [it.id for it in rows] == ["t2", "t1"]
    await gom.indexes.ensure(BY_OPENED_AND_RANK)
    gom.index_advisor.refresh()
    await gom.load_all(Ticket, order_by=[q.opened.asc(), q.rank.asc()])


async def test_says_nothing_when_off_and_warns_once(gom, caplog):
    gom.index_advice = IndexAdvicePolicy.OFF
    with caplog.at_level(logging.WARNING, logger="drivine.dsl"):
        await gom.load_all(Ticket, order_by=q.rank.asc())
        assert caplog.records == []
        gom.index_advice = IndexAdvicePolicy.WARN
        await gom.load_all(Ticket, order_by=q.rank.asc())
        await gom.load_all(Ticket, order_by=q.rank.desc())
    assert [it.getMessage().split(" has")[0] for it in caplog.records] == ["order_by on AdviceTest(rank)"]
