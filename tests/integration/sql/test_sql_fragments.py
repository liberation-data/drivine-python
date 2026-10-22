from collections.abc import AsyncIterator
from typing import Annotated

import pytest

from drivine import GraphProperty, NodeFragment, NodeId, NodeStamp, PersistenceManager, refs
from tests.integration.sql.sql_seed import SqlSeed, raw_property


class Ticket(NodeFragment, labels=["SqlTest", "Ticket"]):
    id: Annotated[str, NodeId()]
    title: str
    number: Annotated[int, GraphProperty("ticket_no")]
    stamp: Annotated[str | None, NodeStamp()] = None


class UrgentTicket(Ticket, labels=["Urgent"]):
    pass


@pytest.fixture
async def seed(pm: PersistenceManager) -> AsyncIterator[SqlSeed]:
    seed = SqlSeed(pm, "SqlTest")
    await seed.prepare()
    await seed.clear()
    await seed.node(["Ticket"], {"id": "t1", "title": "Crash", "ticket_no": 101}, "aaaa:bbbb")
    await seed.node(["Ticket", "Urgent"], {"id": "t2", "title": "Outage", "ticket_no": 102})
    await seed.node(["Ticket"], {"id": "t3", "title": "Typo", "ticket_no": 103})
    await seed.node(["Other"], {"id": "t1", "title": "Not a ticket"})
    yield seed
    await seed.clear()


pytestmark = pytest.mark.usefixtures("seed")


async def test_loads_a_fragment_by_id_by_its_labels_with_its_stamp_and_graph_names(gom):
    ticket = await gom.load(Ticket, "t1")
    assert type(ticket) is Ticket
    assert ticket == Ticket(id="t1", title="Crash", number=101, stamp="aaaa:bbbb")
    assert await gom.load(Ticket, "nobody") is None


async def test_loads_the_most_specific_subtype_a_node_carries_the_labels_of(gom):
    assert type(await gom.load(Ticket, "t2")) is UrgentTicket


async def test_loads_all_filtered_by_sql_over_the_row_n_and_pages(pm, gom):
    assert sorted(it.id for it in await gom.load_all(Ticket)) == ["t1", "t2", "t3"]
    big = await gom.load_all(Ticket, f"{raw_property(pm, 'ticket_no', 'NUMBER')} > $min", {"min": 101})
    assert sorted(it.id for it in big) == ["t2", "t3"]
    assert [it.id for it in await gom.load_all(UrgentTicket)] == ["t2"]


async def test_counts(pm, gom):
    assert await gom.count(Ticket) == 3
    assert await gom.count(Ticket, f"{raw_property(pm, 'title')} = $title", {"title": "Typo"}) == 1


async def test_deletes_by_id_and_condition_and_all_a_filter_finds(pm, gom, seed):
    title = raw_property(pm, "title")
    await seed.node(["Ticket"], {"id": "gone1", "title": "x", "ticket_no": 900})
    await seed.node(["Ticket"], {"id": "gone2", "title": "x", "ticket_no": 901})
    assert await gom.delete(Ticket, "gone1", where=f"{title} = 'y'") == 0
    assert await gom.delete(Ticket, "gone1") == 1
    assert await gom.delete_all(Ticket, f"{title} = 'x'") == 1
    assert await gom.count(Ticket) == 3


async def test_takes_the_query_dsl(gom):
    q = refs(Ticket)
    typed = await gom.load_all(Ticket, q.number.gte(102), order_by=q.number.desc())
    assert [it.id for it in typed] == ["t3", "t2"]
