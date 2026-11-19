"""
Writes a SQLite graph with drivine-python 0.8.0, the last release of store layout 1: the fixture a layout-1 to layout-2
migration is tested on (drivine-cypher-spec#256, drivine-python#514),
`tests/integration/sqlite/migration/layout1-0.8.0.sqlite`.
Run it in an environment of its own, with `pip install "drivine[sqlite]==0.8.0"`, from a directory outside the checkout:
`python -I <checkout>/scripts/write_migration_fixture_0_8_0.py <path of the file to write>`.

drivine4j's `fixture070` and Drivine for TypeScript's `migration-fixture-5.0.0` write the same graph, and this adds what
a Python model saves: a date, a datetime, a float and a relationship's date, each saved by the object manager, and bytes
as base64 text. 0.8.0's object manager can't save bytes on SQLite (it reads them as UTF-8 text, and fails on any that
aren't), so the bytes are written in Cypher, as layout 1's other writers hold them.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, date, datetime
from typing import Annotated, Any

from drivine import (
    DatabaseRegistry,
    DatabaseType,
    GraphView,
    NodeFragment,
    NodeId,
    PersistenceManagerFactory,
    QuerySpecification,
    Relationship,
    RelationshipFragment,
    Root,
    StatelessGraphObjectManager,
    TargetNode,
    uniqueness_constraint,
)
from drivine.query.statement import cypher_statement


class Person(NodeFragment, labels=["Person"]):
    name: Annotated[str, NodeId()]
    born: date | None = None
    height: float | None = None
    visits: int | None = None
    nick: str | None = None


class Meeting(NodeFragment, labels=["Meeting"]):
    id: Annotated[str, NodeId()]
    at: datetime


class Knows(RelationshipFragment):
    since: date
    target: Annotated[Person, TargetNode()]


class Circle(GraphView):
    person: Annotated[Person, Root()]
    knows: Annotated[list[Knows], Relationship("KNOWS")] = []


async def main(path: str) -> None:
    registry = DatabaseRegistry()
    registry.builder().database_type(DatabaseType.SQLITE).database_name(path).register("fixture")
    pm = PersistenceManagerFactory(registry).get("fixture")
    gom = StatelessGraphObjectManager(pm)

    async def cypher(statement: str) -> Any:
        return await pm.execute(QuerySpecification[Any](cypher_statement(statement)))

    try:
        await gom.constraints.ensure(uniqueness_constraint("Person", "name"))
        await gom.constraints.ensure(uniqueness_constraint("Event", "at"))
        await gom.save(Person(name="Ada", born=date(1815, 12, 10), height=1.65, visits=3))
        await gom.save(Person(name="Charles", born=date(1791, 12, 26), height=2.0))
        await gom.save(Person(name="Mary", nick="2024-03-15"))
        await gom.save(
            Circle(person=Person(name="Bob"), knows=[Knows(since=date(2020, 1, 1), target=Person(name="Cy"))])
        )
        await gom.save(Meeting(id="m1", at=datetime(2024, 3, 15, 10, 30, 0, 500000, tzinfo=UTC)))
        await cypher("CREATE (:File {name: 'f1', data: 'AAH+/w=='})")
        await cypher("CREATE (:Robot {`$t`: 1, `$x`: 'y', name: 'R2'})")
        # Two spellings of one moment: distinct texts in layout 1, so its unique index takes both; one value in
        # layout 2.
        await cypher("CREATE (:Event {at: '2024-03-15T10:30:00.5Z'})")
        await cypher("CREATE (:Event {at: '2024-03-15T10:30:00.500Z'})")
    finally:
        await registry.close()
    print(f"wrote {path}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
