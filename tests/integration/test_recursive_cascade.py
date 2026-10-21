"""
A cascade through a view that contains itself deletes what load reads at every level, the nested levels' own
relationships among them: drivine4j's liberation-data/drivine4j#96, which doesn't happen here.
"""

from typing import Annotated

from drivine import (
    CascadeType,
    Direction,
    GraphView,
    NodeFragment,
    NodeId,
    PersistenceManager,
    Relationship,
    Root,
    StatelessGraphObjectManager,
)
from tests.integration.stores import nodes


class Session(NodeFragment, labels=["Session"]):
    id: Annotated[str, NodeId()]


class Message(NodeFragment, labels=["Message"]):
    id: Annotated[str, NodeId()]


class SessionTree(GraphView):
    session: Annotated[Session, Root()]
    messages: Annotated[list[Message], Relationship("HAS_MESSAGE")] = []
    parts: Annotated[list["SessionTree"], Relationship("PART_OF", Direction.INCOMING, max_depth=3)] = []


def tree(id: str, message: str, *parts: SessionTree) -> SessionTree:
    return SessionTree(session=Session(id=id), messages=[Message(id=message)], parts=list(parts))


def ids(view: SessionTree) -> set[str]:
    return {view.session.id, *(it.id for it in view.messages)}.union(*(ids(it) for it in view.parts))


async def test_deletes_what_each_level_holds_as_load_reads_it(pm: PersistenceManager):
    gom = StatelessGraphObjectManager(pm)
    await gom.save(tree("rc-root", "rc-m0", tree("rc-child", "rc-m1", tree("rc-grand", "rc-m2"))))
    loaded = await gom.load(SessionTree, "rc-root")
    assert loaded is not None
    assert ids(loaded) == {"rc-root", "rc-m0", "rc-child", "rc-m1", "rc-grand", "rc-m2"}

    assert await gom.delete(SessionTree, "rc-root", cascade=CascadeType.DELETE_ALL) == 1

    assert await nodes(pm, "Session") == []
    assert await nodes(pm, "Message") == []
