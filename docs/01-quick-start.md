## Quick start

Describe the database in the environment:

```
DATABASE_TYPE=NEO4J
DATABASE_HOST=localhost
DATABASE_USER=neo4j
DATABASE_PASSWORD=…
```

Declare a fragment for each node a use case reads, and a view that composes them:

```python
from typing import Annotated

from drivine import GraphView, NodeFragment, NodeId, Relationship, RelationshipFragment, Root, TargetNode


class Person(NodeFragment, labels=["Person"]):
    uuid: Annotated[str, NodeId()]
    name: str
    bio: str | None = None


class Organization(NodeFragment, labels=["Organization"]):
    uuid: Annotated[str, NodeId()]
    name: str


class WorkHistory(RelationshipFragment):
    role: str
    organization: Annotated[Organization, TargetNode()]


class PersonCareer(GraphView):
    person: Annotated[Person, Root()]
    employment_history: Annotated[list[WorkHistory], Relationship("WORKS_FOR")] = []
```

Then load and save, each in one statement:

```python
import asyncio

from drivine import DrivineAssembly, refs


async def main() -> None:
    async with DrivineAssembly() as drivine:
        gom = drivine.graph_object_manager()
        await gom.save(
            PersonCareer(
                person=Person(uuid="p1", name="Ada", bio="Design Lead"),
                employment_history=[WorkHistory(role="Lead", organization=Organization(uuid="o1", name="Studio"))],
            )
        )
        q = refs(PersonCareer)
        for career in await gom.load_all(PersonCareer, where=q.person.bio.contains("Lead")):
            print(career.person.name, [it.organization.name for it in career.employment_history])


asyncio.run(main())
```

`DrivineAssembly` registers the database the environment describes and holds the managers. Entering it starts Drivine,
holding [the schema](07-schema.md#schema) if you declare one, and leaving it closes the drivers and pools, as Spring starts and
closes drivine4j with its context. In an application, [your own assembly holds it](09-with-pi-pilgrim.md#with-pi-pilgrim), and closes it
when it closes.

Or, without the assembly, register the database yourself. Leaving the registry closes it:

```python
from drivine import DatabaseRegistry, PersistenceManagerFactory, StatelessGraphObjectManager

async with DatabaseRegistry() as registry:
    registry.build_or_resolve_from_env()
    gom = StatelessGraphObjectManager(PersistenceManagerFactory(registry).get())
    ...
```

From here: [the persistence manager](04-statements.md#statements) for Cypher of your own, [transactions](05-transactions.md#transactions),
[queries](06-fragments-and-views.md#queries) and [typed references](06-fragments-and-views.md#typed-references), [the sync API](08-sync.md#sync), and
[DrivineAssembly](09-with-pi-pilgrim.md#with-pi-pilgrim) to wire it into an application.
