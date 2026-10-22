# Written by scripts/unasync.py from tests/integration/sql/test_sql_fragments.py:
# change that, and run the script again.
from collections.abc import Iterator
from typing import Annotated

import pytest

from drivine.sync import GraphProperty, NodeFragment, NodeId, NodeStamp, PersistenceManager, refs
from tests.sync_integration.sql.sql_seed import SqlSeed, raw_property


class Ticket(NodeFragment, labels=["SqlTest", "Ticket"]):
    id: Annotated[str, NodeId()]
    title: str
    number: Annotated[int, GraphProperty("ticket_no")]
    stamp: Annotated[str | None, NodeStamp()] = None


class UrgentTicket(Ticket, labels=["Urgent"]):
    pass


@pytest.fixture
def seed(pm: PersistenceManager) -> Iterator[SqlSeed]:
    seed = SqlSeed(pm, "SqlTest")
    seed.prepare()
    seed.clear()
    seed.node(["Ticket"], {"id": "t1", "title": "Crash", "ticket_no": 101}, "aaaa:bbbb")
    seed.node(["Ticket", "Urgent"], {"id": "t2", "title": "Outage", "ticket_no": 102})
    seed.node(["Ticket"], {"id": "t3", "title": "Typo", "ticket_no": 103})
    seed.node(["Other"], {"id": "t1", "title": "Not a ticket"})
    yield seed
    seed.clear()


pytestmark = pytest.mark.usefixtures("seed")


def test_loads_a_fragment_by_id_by_its_labels_with_its_stamp_and_graph_names(gom):
    ticket = gom.load(Ticket, "t1")
    assert type(ticket) is Ticket
    assert ticket == Ticket(id="t1", title="Crash", number=101, stamp="aaaa:bbbb")
    assert gom.load(Ticket, "nobody") is None


def test_loads_the_most_specific_subtype_a_node_carries_the_labels_of(gom):
    assert type(gom.load(Ticket, "t2")) is UrgentTicket


def test_loads_all_filtered_by_sql_over_the_row_n_and_pages(pm, gom):
    assert sorted(it.id for it in gom.load_all(Ticket)) == ["t1", "t2", "t3"]
    big = gom.load_all(Ticket, f"{raw_property(pm, 'ticket_no', 'NUMBER')} > $min", {"min": 101})
    assert sorted(it.id for it in big) == ["t2", "t3"]
    assert [it.id for it in gom.load_all(UrgentTicket)] == ["t2"]


def test_counts(pm, gom):
    assert gom.count(Ticket) == 3
    assert gom.count(Ticket, f"{raw_property(pm, 'title')} = $title", {"title": "Typo"}) == 1


def test_deletes_by_id_and_condition_and_all_a_filter_finds(pm, gom, seed):
    title = raw_property(pm, "title")
    seed.node(["Ticket"], {"id": "gone1", "title": "x", "ticket_no": 900})
    seed.node(["Ticket"], {"id": "gone2", "title": "x", "ticket_no": 901})
    assert gom.delete(Ticket, "gone1", where=f"{title} = 'y'") == 0
    assert gom.delete(Ticket, "gone1") == 1
    assert gom.delete_all(Ticket, f"{title} = 'x'") == 1
    assert gom.count(Ticket) == 3


def test_takes_the_query_dsl(gom):
    q = refs(Ticket)
    typed = gom.load_all(Ticket, q.number.gte(102), order_by=q.number.desc())
    assert [it.id for it in typed] == ["t3", "t2"]
