"""The order a batch writes its fragments in: the classes by name and the rows of each by id, whatever order given."""

from typing import Annotated, Any, TypeVar

import pytest

from drivine import DatabaseType, NodeFragment, NodeId, NodeStamp, QuerySpecification, StatelessGraphObjectManager
from drivine.backend.fragment_group import FragmentGroup
from drivine.backend.graph_statement import GraphStatement
from drivine.cursor.cursor import Cursor
from drivine.cursor.cursor_specification import CursorSpecification
from drivine.manager.persistence_manager import PersistenceManager

T = TypeVar("T")


class Ledger(NodeFragment, labels=["Ledger"]):
    id: Annotated[str, NodeId()]
    total: int = 1


class Bookmark(NodeFragment, labels=["Bookmark"]):
    id: Annotated[str, NodeId()]
    cursor: str = ""
    stamp: Annotated[str | None, NodeStamp()] = None


class ChunkHash(NodeFragment, labels=["ChunkHash"]):
    id: Annotated[str, NodeId()]
    chunk: str = ""


def stamp_of(node_id: str) -> str:
    """The stamp the store left on the node with the id."""
    return f"stamp-of-{node_id}"


class Recording(PersistenceManager):
    """
    Records the statements it runs, with their parameters, and answers a batch of fragments with the stamp of each id
    it binds. No provider is registered for it, so a batch of one statement runs alone.
    """

    def __init__(self, type_: DatabaseType = DatabaseType.NEO4J) -> None:
        self.database = "recording"
        self.type = type_
        self.ran: list[tuple[str, dict[str, Any]]] = []

    async def query(self, spec: QuerySpecification[T]) -> list[T]:
        assert spec.statement is not None
        parameters: dict[str, Any] = dict(spec.parameters) if isinstance(spec.parameters, dict) else {}
        self.ran.append((spec.statement.text, parameters))
        ids = [value for key, value in parameters.items() if key.startswith("ids")]
        groups = [{"before": [], "after": [{"id": it, "stamp": stamp_of(it)} for it in group]} for group in ids]
        return [{"groups": groups}]  # type: ignore[list-item]

    async def open_cursor(self, spec: CursorSpecification[T]) -> Cursor[T]:
        raise NotImplementedError


async def test_writes_the_classes_of_one_statement_in_the_order_of_their_names_whatever_order_they_are_given_in():
    pm = Recording()
    gom = StatelessGraphObjectManager(pm)
    await gom.save_all([Ledger(id="l1"), Bookmark(id="b1"), ChunkHash(id="h1")])
    await gom.save_all([ChunkHash(id="h2"), Bookmark(id="b2"), Ledger(id="l2")])

    orders = [
        sorted(["Ledger", "Bookmark", "ChunkHash"], key=lambda label, text=text: text.index(f"MERGE (n:{label}"))
        for text, _ in pm.ran
    ]
    assert orders == [["Bookmark", "ChunkHash", "Ledger"], ["Bookmark", "ChunkHash", "Ledger"]]


async def test_writes_the_rows_of_each_class_of_one_statement_in_the_order_of_their_ids():
    pm = Recording()
    await StatelessGraphObjectManager(pm).save_all(
        [ChunkHash(id="h2"), Ledger(id="l1"), ChunkHash(id="h1"), ChunkHash(id="h3")]
    )

    _, parameters = pm.ran[0]
    assert [it["id"] for it in parameters["rows0"]] == ["h1", "h2", "h3"]
    assert parameters["ids0"] == ["h1", "h2", "h3"]


async def test_keeps_the_rows_of_one_id_in_the_order_they_were_given_in():
    pm = Recording()
    await StatelessGraphObjectManager(pm).save_all([Ledger(id="l2"), Ledger(id="l1"), Ledger(id="l1", total=2)])

    _, parameters = pm.ran[0]
    assert [(it["id"], it["props"]["total"]) for it in parameters["rows0"]] == [("l1", 1), ("l1", 2), ("l2", 1)]


async def test_returns_the_objects_in_the_order_given_each_with_its_own_stamp():
    objects = [Bookmark(id="b3"), Ledger(id="l1"), Bookmark(id="b1"), Bookmark(id="b2")]

    saved = await StatelessGraphObjectManager(Recording()).save_all(objects)

    assert [it.id for it in saved] == ["b3", "l1", "b1", "b2"]
    assert all(it is given for it, given in zip(saved, objects, strict=True))
    assert [it.stamp for it in saved if isinstance(it, Bookmark)] == [stamp_of(it) for it in ["b3", "b1", "b2"]]


@pytest.mark.parametrize("type_", [DatabaseType.NEO4J, DatabaseType.ORACLE, DatabaseType.POSTGRES])
async def test_hands_the_backend_its_classes_in_the_order_of_their_names_and_the_objects_of_each_by_id(type_):
    gom = StatelessGraphObjectManager(Recording(type_))
    given: list[list[FragmentGroup]] = []

    def fragment_batch(groups: list[FragmentGroup], options: Any) -> GraphStatement:
        given.append(groups)
        return GraphStatement("BATCH", {f"ids{i}": [it.id for it in group.objects] for i, group in enumerate(groups)})

    gom.backend.fragment_batch = fragment_batch  # type: ignore[method-assign]

    await gom.save_all([Ledger(id="l2"), Bookmark(id="b2"), Ledger(id="l1"), Bookmark(id="b1")])

    assert [[(it.model.type.__name__, [o.id for o in it.objects]) for it in groups] for groups in given] == [
        [("Bookmark", ["b1", "b2"]), ("Ledger", ["l1", "l2"])]
    ]
