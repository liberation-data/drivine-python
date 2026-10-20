# Written by scripts/unasync.py from tests/integration/oracle/test_oracle_fragments.py:
# change that, and run the script again.
import json
from collections.abc import Iterator
from typing import Annotated, Any

import pytest

from drivine.sync import GraphProperty, NodeFragment, NodeId, NodeStamp, PersistenceManager, QuerySpecification, refs
from tests.sync_integration.oracle.oracle_seed import ON_ORACLE


class Ticket(NodeFragment, labels=["OraTest", "Ticket"]):
    id: Annotated[str, NodeId()]
    title: str
    number: Annotated[int, GraphProperty("ticket_no")]
    stamp: Annotated[str | None, NodeStamp()] = None


class UrgentTicket(Ticket, labels=["Urgent"]):
    pass


def node(pm: PersistenceManager, labels: list[str], props: dict[str, Any], stamp: str | None = None) -> None:
    pm.execute(
        QuerySpecification[Any](
            "INSERT INTO drivine_node (labels, props, stamp) VALUES (JSON($labels), JSON($props), $stamp)"
        ).bind({"labels": json.dumps(labels), "props": json.dumps(props), "stamp": stamp})
    )


@pytest.fixture
def tickets(pm: PersistenceManager) -> Iterator[None]:
    clear = QuerySpecification[Any]("DELETE FROM drivine_node n WHERE JSON_EXISTS(n.labels, '$?(@ == \"OraTest\")')")
    pm.execute(clear)
    node(pm, ["OraTest", "Ticket"], {"id": "t1", "title": "Crash", "ticket_no": 101}, "aaaa:bbbb")
    node(pm, ["OraTest", "Ticket", "Urgent"], {"id": "t2", "title": "Outage", "ticket_no": 102})
    node(pm, ["OraTest", "Ticket"], {"id": "t3", "title": "Typo", "ticket_no": 103})
    node(pm, ["OraTest", "Other"], {"id": "t1", "title": "Not a ticket"})
    yield
    pm.execute(clear)


pytestmark = [ON_ORACLE, pytest.mark.usefixtures("tickets")]


def test_loads_a_fragment_by_id_by_its_labels_with_its_stamp_and_graph_names(gom):
    ticket = gom.load(Ticket, "t1")
    assert type(ticket) is Ticket
    assert ticket == Ticket(id="t1", title="Crash", number=101, stamp="aaaa:bbbb")
    assert gom.load(Ticket, "nobody") is None


def test_loads_the_most_specific_subtype_a_node_carries_the_labels_of(gom):
    assert type(gom.load(Ticket, "t2")) is UrgentTicket


def test_loads_all_filtered_by_sql_over_the_row_n_and_pages(gom):
    assert sorted(it.id for it in gom.load_all(Ticket)) == ["t1", "t2", "t3"]
    big = gom.load_all(Ticket, "JSON_VALUE(n.props, '$.ticket_no' RETURNING NUMBER) > $min", {"min": 101})
    assert sorted(it.id for it in big) == ["t2", "t3"]
    assert [it.id for it in gom.load_all(UrgentTicket)] == ["t2"]


def test_counts(gom):
    assert gom.count(Ticket) == 3
    assert gom.count(Ticket, "JSON_VALUE(n.props, '$.title') = $title", {"title": "Typo"}) == 1


def test_deletes_by_id_and_condition_and_all_a_filter_finds(pm, gom):
    node(pm, ["OraTest", "Ticket"], {"id": "gone1", "title": "x", "ticket_no": 900})
    node(pm, ["OraTest", "Ticket"], {"id": "gone2", "title": "x", "ticket_no": 901})
    assert gom.delete(Ticket, "gone1", where="JSON_VALUE(n.props, '$.title') = 'y'") == 0
    assert gom.delete(Ticket, "gone1") == 1
    assert gom.delete_all(Ticket, "JSON_VALUE(n.props, '$.title') = 'x'") == 1
    assert gom.count(Ticket) == 3


def test_takes_the_query_dsl(gom):
    q = refs(Ticket)
    typed = gom.load_all(Ticket, q.number.gte(102), order_by=q.number.desc())
    assert [it.id for it in typed] == ["t3", "t2"]
