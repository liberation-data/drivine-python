# Drivine for Python

**A graph database client for Python**, beside [Drivine](https://github.com/liberation-data/drivine) for TypeScript
and [drivine4j](https://github.com/liberation-data/drivine4j) for the JVM. Async first, typed, and fast: one statement per
round trip, no session cache, no lazy loading.

> **Status: early.** On Neo4j and Memgraph today: the low-level `PersistenceManager`, transactions, and the object
> manager's loading, saving, batches, patches, edges and deletes of fragments and views. The query DSL and the other
> engines come next. Neptune connects
> the same way, for the `PersistenceManager`. See [the design](docs/design.md) for where it is going.

```
pip install "drivine[neo4j]"   # Neo4j, Memgraph, Neptune
```

Python 3.11 or newer.

## Connecting

Describe a database in the environment, with the same variables as TypeScript's Drivine and drivine4j, so one `.env`
serves an application in any of them:

```
DATABASE_TYPE=NEO4J
DATABASE_HOST=localhost
DATABASE_PORT=7687
DATABASE_USER=neo4j
DATABASE_PASSWORD=…
```

A second database takes a prefix, `ANALYTICS_DATABASE_TYPE` and so on. Then register them:

```python
from drivine import DatabaseRegistry, PersistenceManagerFactory

registry = DatabaseRegistry()
registry.build_or_resolve_from_env()              # "default"
registry.build_or_resolve_from_env("analytics")   # ANALYTICS_DATABASE_*

pm = PersistenceManagerFactory(registry).get()    # the default database
...
await registry.close()                            # when the application stops
```

Or in code:

```python
registry.builder().database_type(DatabaseType.MEMGRAPH).host("localhost").register("graph")
```

Drivine reads `os.environ`, and doesn't load `.env` files itself: use python-dotenv, pydantic-settings, or your
platform's way.

## Statements

The `PersistenceManager` runs your statements and maps their results:

```python
from drivine import QuerySpecification

spec = (QuerySpecification[Person]()
    .with_statement("MATCH (p:Person {city: $city}) RETURN properties(p)")
    .bind({"city": "Sydney"})    # named parameters, or a list for $1, $2, …
    .transform(Person)           # validated with Pydantic: a model, dataclass, TypedDict…
    .filter(lambda it: it.age >= 18)
    .limit(10))

people = await pm.query(spec)
person = await pm.get_one(spec)          # exactly one, or it raises
maybe = await pm.maybe_get_one(spec)     # one or None
await pm.execute(spec)                   # no results
```

- A node or relationship comes back as its properties, and Neo4j's dates and times as the standard library's.
- A row of one column is its value, and a row of several is a list.
- A failed statement raises `DrivineError`, which holds the specification, and the database's error as its cause.

### Cursors

A cursor streams a large result, reading it from the driver `batch_size` rows at a time:

```python
spec = CursorSpecification[Route]("MATCH (r:Route) RETURN properties(r)").batch_size(500).transform(Route)
async with await pm.open_cursor(spec) as cursor:
    async for route in cursor:
        ...
```

It works in a transaction or outside one, where it has a connection of its own until it is closed.

### Statements in files

```python
from drivine import cypher_file

routes_between = cypher_file(__package__, "routes_between.cypher")
```

Read with `importlib.resources`, so it works from an installed wheel as well as a source tree.

## Transactions

```python
from drivine import transactional


class IssueService:
    def __init__(self, issues: IssueRepository, audit: AuditLog):
        self.issues = issues
        self.audit = audit

    @transactional()           # join the current transaction, or start one
    async def raise_issue(self, issue: Issue) -> Issue:
        saved = await self.issues.save(issue)
        await self.audit.record(saved)   # same transaction
        return saved
```

Or for a block:

```python
from drivine import transaction

async with transaction():
    await pm.execute(first)
    await pm.execute(second)

async with transaction(rollback=True):   # a test that leaves nothing behind
    ...
```

- **The outermost call or block owns the transaction.** It commits when it ends, and rolls back when an exception
  leaves it. Inner ones join it.
- **A failure anywhere rolls the whole transaction back.** An exception that leaves any `@transactional` call, even
  one the caller catches, marks the transaction rollback-only. It then rolls back, and its end raises
  `TransactionRolledBackError` rather than committing what the inner call half did. `no_rollback_for=(NotFound,)`
  exempts an exception.
- **One transaction spans every database it touches.** It opens a connection to each, and begins its transaction,
  the first time it runs a statement there. Commits are one after another, not two-phase.
- **A `@transactional` method that never reaches Drivine costs nothing**: no connection is opened. Code with fakes in
  its tests, or with Drivine taken out, runs as written.
- **Outside a transaction**, each statement runs on its own and commits. Drivine never starts a transaction you didn't
  ask for.
- **It works on calls to `self`**, since it wraps the function itself, not a proxy.

### Tasks share their transaction, one statement at a time

The current transaction lives in a context variable, so asyncio carries it across every `await` and into the tasks
started inside it:

```python
@transactional()
async def import_all(self, people: list[Person]) -> None:
    async with asyncio.TaskGroup() as tasks:
        for person in people:
            tasks.create_task(self.people.add(person))   # all in this transaction
```

A database transaction runs one statement at a time, so Drivine queues them, in the order they were issued. A
cursor takes its turn for each row it reads, so other statements run between its rows. A statement that fails dooms
the transaction, and the statements after it raise `TransactionRolledBackError` at once.

The queue keeps concurrency correct, not parallel. For a parallel import, give each task its own transaction.

## Fragments and views

Map a node with a `NodeFragment`, and compose fragments and relationships into a `GraphView`, which loads in one
statement:

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


class WorkHistory(RelationshipFragment):     # a relationship's properties, and the node it leads to
    start_date: str
    role: str
    target: Annotated[Organization, TargetNode()]


class PersonCareer(GraphView):
    person: Annotated[Person, Root()]
    employment_history: Annotated[list[WorkHistory], Relationship("WORKS_FOR")] = []
```

```python
gom = drivine.graph_object_manager()        # from DrivineAssembly, or StatelessGraphObjectManager(pm)

career = await gom.load(PersonCareer, uuid)
leads = await gom.load_all(PersonCareer, "n.bio CONTAINS $text", {"text": "Lead"})
total = await gom.count(Person)
```

- **Models are Pydantic.** A loaded fragment is validated, so a bad value fails at the load.
- **The annotation says what a relationship holds**: a fragment, a relationship fragment or a view, one or a list. A
  single relationship typed without `None` is required, so only roots that have it load; `Person | None` is
  optional. A list is required with `Relationship(..., required=True)`.
- **Properties are stored in camel case**, as TypeScript and JVM models store them: `start_date` as `startDate`.
  `GraphProperty("name")` names one, and `property_names="as_written"` keeps a class's fields as they are written.
- **A subclass is loaded as itself.** A fragment's labels are inherited, and a node loads as the most specific
  subclass whose labels it carries.
- **Recursion:** `Relationship("HAS_LOCATION", max_depth=3)` on a list of the view's own class expands three levels;
  `depth={"sub_locations": 5}` overrides it for one load.
- **More markers:** `GraphPath([hop(...), ...])` for the nodes at the end of several hops, `Count` and `Aggregate`
  computed in the query, `SortedBy`, `ReadOnly`, `Transient`, `NodeStamp`, `NodeLabels`, `PropertyBag`,
  `JsonPacked`, `NullAsDefault` and `EmptyWhenAbsent`, as in TypeScript's Drivine.

The statements are TypeScript's, character for character: the tests check them against those TypeScript's Drivine
writes for the same models.

### Saving

```python
saved = await gom.save(career)                                       # fields that aren't None; relationships added
await gom.save(career, replace=["employment_history"])               # this list is the whole list
await gom.save(person, only=["name"])                                # or except_=["embedding"]
await gom.save(person, null_policy=NullPolicy.CLEAR)                 # a field assigned None clears its property
created = await gom.create(person)                                   # only if absent; None when it was there
updated = await gom.update(Person, uuid, lambda it: it.model_copy(update={"name": "Ada"}))
await gom.delete(PersonCareer, uuid, cascade=CascadeType.DELETE_ORPHAN)
await gom.delete_all(Person, "n.name = $name", {"name": "Ada"})

await gom.save_all(chunks)                                           # fragments in one statement per 1,000
result = await gom.patch(Session, session_id, set={"last_activity_at": now},
                         where="n.lastActivityAt IS NULL OR n.lastActivityAt < $now", bindings={"now": now})
results = await gom.patch_all(chunks, only=["embedding"])            # named fields, where each node is there

await gom.edges.relate(node_ref(Issue, a), node_ref(Person, b), "ASSIGNED_TO")
await gom.edges.unrelate(node_ref(Issue, a), node_ref(Person, b), "ASSIGNED_TO")
people = await gom.edges.load_related(node_ref(Issue, a), "ASSIGNED_TO", Person)
```

- **A save is one statement**, applied whole or not at all: the root, the relationships it drops, each related node
  and the relationship to it.
- **A save adds and removes nothing**, unless a field is named in `replace`. `removed_targets=DELETE_UNREFERENCED`
  also deletes a removed node that nothing else refers to.
- **Stamps refuse a stale save.** On a class with a `NodeStamp` field, a save of an object loaded before another
  writer changed the node raises `StaleObjectError` and writes nothing. Use the object a save returns: it carries the
  new stamp. `update` loads, applies your change and saves what it altered, again if the node changed meanwhile.
- **`save_all`** runs its statements in one transaction, or in yours, so a failure leaves nothing written.
- **`patch`** writes named fields of one node by id, if it meets a condition, without loading it, and says whether it
  found the node, applied the patch and changed anything. It never makes a node.
- **Edges** relate and unrelate stored nodes by id, marking both ends' stamps.
- **Outside a transaction**, a save the engine turns away because another writer held the node is run again after a
  pause. Inside one, it joins the transaction, and fails for its owner to decide.

The store format, stamps included, is TypeScript's and drivine4j's, so the three can share a store.

## With pi-pilgrim

`DrivineAssembly` is a [pi-pilgrim](https://github.com/liberation-data/pi-pilgrim) assembly holding the registry
and the managers, for your own assembly to use:

```python
from pipilgrim import AssemblyHolder, PilgrimAssembly
from drivine import DrivineAssembly


class AppAssembly(PilgrimAssembly):
    def __init__(self):
        self.drivine = AssemblyHolder.shared(DrivineAssembly)
        super().__init__()

    def route_repository(self) -> RouteRepository:
        return self.shared(lambda: RouteRepository(self.drivine.persistence_manager()))

    def report_repository(self) -> ReportRepository:
        return self.shared(lambda: ReportRepository(self.drivine.persistence_manager("analytics")))
```

- It registers the database the environment describes. To register yours in code, subclass it and override
  `register_databases(registry)`.
- `Assembled` injects a manager into a class something else makes:
  `pm: PersistenceManager = Assembled(assembly=DrivineAssembly)`.
- Call `await assembly.drivine.close()` when the application stops, in FastAPI's lifespan, say.

Nothing requires pi-pilgrim: the managers are plain classes.

## Testing

`pip install "drivine[testing]"` adds a pytest plugin. With

```toml
[tool.pytest.ini_options]
drivine_test_containers = true
```

it starts a container for each datasource the environment configures, Neo4j or Memgraph, before the tests run, and
points the datasource at it. Datasources of one type share a container. `USE_LOCAL_NEO4J=true` (or `_MEMGRAPH`)
leaves that type's datasources as they are, to test against your own instance, and `NEO4J_TEST_IMAGE` picks another
image.

## Logging

Drivine logs through the standard `logging` module: `drivine.statements` logs each statement at DEBUG, with its
session and how long it took. `DRIVINE_LOG_LEVEL`, as TypeScript's Drivine reads it, sets the level of the `drivine`
logger when the application hasn't.

## Building

```
uv sync
uv run pytest            # starts Neo4j and Memgraph in Docker
uv run ruff check . && uv run pyright
```

## License

Apache 2.0.
