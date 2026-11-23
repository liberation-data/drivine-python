"""
Writes a graph with drivine-python 0.8.0, the last release of store layout 1: the fixture a layout-1 to layout-2
migration is tested on (drivine-cypher-spec#256, drivine-python#514). On SQLite it is a file,
`tests/integration/sqlite/migration/layout1-0.8.0.sqlite`; on Postgres and Oracle the migration's tests write it into
the test's own database, a graph of each test's own (tests/integration/migration).

Run it in an environment of its own, with `drivine[sqlite]==0.8.0` (or `[postgres]`, `[oracle]`) installed, from a
directory outside the checkout, so that this checkout's drivine isn't the one imported:

    python -I <checkout>/scripts/write_migration_fixture_0_8_0.py sqlite <path of the file to write> [graph]
    python -I <checkout>/scripts/write_migration_fixture_0_8_0.py postgres|oracle host port user password database graph

where Oracle's database is its service, say FREEPDB1, and the graph is `drivine` where it isn't given.

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


STORES = {"sqlite": DatabaseType.SQLITE, "postgres": DatabaseType.POSTGRES, "oracle": DatabaseType.ORACLE}


def registered(arguments: list[str]) -> tuple[DatabaseRegistry, str]:
    """A registry of the store and graph the arguments name, and what to call the graph written."""
    usage = "usage: sqlite <path> [graph] | postgres|oracle <host> <port> <user> <password> <database> <graph>"
    if not arguments or arguments[0] not in STORES:
        raise SystemExit(usage)
    store, rest = STORES[arguments[0]], arguments[1:]
    registry = DatabaseRegistry()
    builder = registry.builder().database_type(store)
    if store is DatabaseType.SQLITE:
        if len(rest) not in (1, 2):
            raise SystemExit(usage)
        builder.database_name(rest[0])
        graph = rest[1] if len(rest) == 2 else "drivine"
        written = rest[0]
    else:
        if len(rest) != 6:
            raise SystemExit(usage)
        host, port, user, password, database, graph = rest
        builder.host(host).port(int(port)).user_name(user).password(password).database_name(database)
        written = f"{arguments[0]} {host}:{port}/{database}"
    builder.default_graph_path(graph).register("fixture")
    return registry, f"{written}, graph {graph}"


async def main(arguments: list[str]) -> None:
    registry, written = registered(arguments)
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
    print(f"wrote {written}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
