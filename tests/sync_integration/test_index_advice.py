# Written by scripts/unasync.py from tests/integration/test_index_advice.py:
# change that, and run the script again.
import logging
from typing import Annotated

import pytest

from drivine.sync import (
    DrivineError,
    IndexAdvicePolicy,
    NodeFragment,
    NodeId,
    PersistenceManager,
    StatelessGraphObjectManager,
    range_index,
    refs,
)
from tests.sync_integration.stores import create


class Ticket(NodeFragment, labels=["AdviceTest"]):
    id: Annotated[str, NodeId()]
    opened: int
    rank: int


BY_OPENED = range_index("AdviceTest", "opened")
BY_OPENED_AND_RANK = range_index("AdviceTest", ["opened", "rank"])


@pytest.fixture
def gom(pm: PersistenceManager):
    gom = StatelessGraphObjectManager(pm)
    gom.index_advice = IndexAdvicePolicy.FAIL
    gom.indexes.drop(BY_OPENED_AND_RANK)
    gom.indexes.drop(BY_OPENED)
    create(pm, "AdviceTest", {"id": "t1", "opened": 1, "rank": 2}, {"id": "t2", "opened": 2, "rank": 1})
    yield gom
    gom.indexes.drop(BY_OPENED_AND_RANK)
    gom.indexes.drop(BY_OPENED)


q = refs(Ticket)


def test_refuses_an_unindexed_order_when_told_to_fail_naming_the_index_to_make(gom):
    with pytest.raises(
        DrivineError,
        match=r"order_by on AdviceTest\(opened\) has no matching range index"
        r".*range_index\('AdviceTest', \['opened'\]\)",
    ):
        gom.load_all(Ticket, order_by=q.opened.asc())
    with pytest.raises(DrivineError, match=r"seek on AdviceTest\(opened\)"):
        gom.load_all(Ticket, order_by=q.opened.asc(), seek=q.opened.after(1))


def test_does_not_advise_an_order_of_one_root_pinned_by_its_id(gom):
    one = gom.load_all(Ticket, where=q.id.eq("t1"), order_by=q.opened.asc())
    assert [it.id for it in one] == ["t1"]


def test_takes_an_index_over_exactly_the_ordered_properties_its_own_at_once_another_once_refreshed(gom):
    with pytest.raises(DrivineError, match="no matching"):
        gom.load_all(Ticket, order_by=q.opened.asc())
    gom.indexes.ensure(BY_OPENED)
    rows = gom.load_all(Ticket, order_by=q.opened.desc())
    assert [it.id for it in rows] == ["t2", "t1"]
    StatelessGraphObjectManager(gom.persistence_manager).indexes.ensure(BY_OPENED_AND_RANK)
    with pytest.raises(DrivineError, match="no matching"):  # the list read before another made the index is kept
        gom.load_all(Ticket, order_by=[q.opened.asc(), q.rank.asc()])
    gom.index_advisor.refresh()
    gom.load_all(Ticket, order_by=[q.opened.asc(), q.rank.asc()])


def test_says_nothing_when_off_and_warns_once(gom, caplog):
    gom.index_advice = IndexAdvicePolicy.OFF
    with caplog.at_level(logging.WARNING, logger="drivine.dsl"):
        gom.load_all(Ticket, order_by=q.rank.asc())
        assert caplog.records == []
        gom.index_advice = IndexAdvicePolicy.WARN
        gom.load_all(Ticket, order_by=q.rank.asc())
        gom.load_all(Ticket, order_by=q.rank.desc())
    assert [it.getMessage().split(" has")[0] for it in caplog.records] == ["order_by on AdviceTest(rank)"]
