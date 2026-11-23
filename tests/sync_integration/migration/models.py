# Written by scripts/unasync.py from tests/integration/migration/models.py:
# change that, and run the script again.
"""
The models of the graph drivine-python 0.8.0 wrote in store layout 1 (scripts/write_migration_fixture_0_8_0.py), as this
version reads them, and the declarations a migration of it is run with: shared by the migration's tests on each store.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated

from drivine.sync import GraphView, NodeFragment, NodeId, Relationship, RelationshipFragment, Root, TargetNode
from drivine.sync.migration.store_layout_migration import Declaration, declarations_of, with_declared


class Person(NodeFragment, labels=["Person"]):
    name: Annotated[str, NodeId()]
    born: date | None = None
    height: float | None = None
    visits: int | None = None
    nick: str | None = None


class Meeting(NodeFragment, labels=["Meeting"]):
    id: Annotated[str, NodeId()]
    at: datetime


class File(NodeFragment, labels=["File"]):
    name: Annotated[str, NodeId()]
    data: bytes


class Knows(RelationshipFragment):
    since: date
    target: Annotated[Person, TargetNode()]


class Circle(GraphView):
    person: Annotated[Person, Root()]
    knows: Annotated[list[Knows], Relationship("KNOWS")] = []


DECLARATIONS = with_declared(
    [
        *declarations_of(Person),
        *declarations_of(Meeting),
        *declarations_of(File),
        *declarations_of(Knows, "KNOWS"),
    ],
    # The Events were written in Cypher, and have no model to say what they hold; nor are bytes ever derived, as 0.8.0's
    # object manager saved them as their UTF-8 text (see _KINDS): File's were written in Cypher as base64, so are
    # declared by hand.
    [Declaration("Event", "at", "datetime"), Declaration("File", "data", "bytes")],
)
