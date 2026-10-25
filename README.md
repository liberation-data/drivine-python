<a href="https://drivine.org/"><img src="https://raw.githubusercontent.com/liberation-data/drivine/master/website/public/images/splash.jpg" alt="Drivine: graph database client for TypeScript, Python and the JVM"></a>

# drivine-python

[![PyPI](https://img.shields.io/pypi/v/drivine.svg)](https://pypi.org/project/drivine/)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)

**Docs: [drivine.org](https://drivine.org/py/)**, for Python, TypeScript, and Kotlin & Java.

A graph database client for Python, supporting **Neo4j**, **Memgraph**, **FalkorDB**, **Oracle** (SQL/PGQ) and
**Postgres** as graph stores, plus **Amazon Neptune** through the low-level API. Async first, with a sync API written from it, typed, and fast: one statement per round trip, no session
cache, no lazy loading. It gives you two ways to work with a graph:

1. **PersistenceManager**: a low-level API where you write the queries yourself.
2. **Object manager** (`StatelessGraphObjectManager`): a high-level API that maps Pydantic models, with a query DSL
   whose typed references are stubs generated from your models.

Neptune connects for the `PersistenceManager`; the object manager runs on the others. 0.1 is an alpha release: the API
may still change. See [the design](docs/design.md) for how it came about.

> **On Node.js or the JVM?** [Drivine](https://github.com/liberation-data/drivine)
> (`npm install @liberation-data/drivine`) and [drivine4j](https://github.com/liberation-data/drivine4j)
> (`org.drivine:drivine4j`) are its TypeScript and Kotlin & Java twins, with the same object manager, query DSL, schema
> management and engines. The three share a storage format: a store written from any of them is read, stamped and
> checked alike by the others.

```
pip install "drivine[neo4j]"      # Neo4j, Memgraph, Neptune
pip install "drivine[falkordb]"   # FalkorDB
pip install "drivine[oracle]"     # Oracle 23ai and 26ai, through python-oracledb in thin mode
pip install "drivine[postgres]"   # Postgres 14 or later, through psycopg 3
pip install "drivine[pilgrim]"    # DrivineAssembly, for pi-pilgrim; optional
```

Python 3.11 or newer.

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

from drivine import DatabaseRegistry, PersistenceManagerFactory, StatelessGraphObjectManager, refs


async def main() -> None:
    registry = DatabaseRegistry()
    registry.build_or_resolve_from_env()
    gom = StatelessGraphObjectManager(PersistenceManagerFactory(registry).get())
    try:
        await gom.save(
            PersonCareer(
                person=Person(uuid="p1", name="Ada", bio="Design Lead"),
                employment_history=[WorkHistory(role="Lead", organization=Organization(uuid="o1", name="Studio"))],
            )
        )
        q = refs(PersonCareer)
        for career in await gom.load_all(PersonCareer, where=q.person.bio.contains("Lead")):
            print(career.person.name, [it.organization.name for it in career.employment_history])
    finally:
        await registry.close()


asyncio.run(main())
```

From here: [the persistence manager](#statements) for Cypher of your own, [transactions](#transactions),
[queries](#queries) and [typed references](#typed-references), [the sync API](#sync), and
[DrivineAssembly](#with-pi-pilgrim) to wire it into an application.

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

On FalkorDB, `DATABASE_NAME` names the graph, `graph` by default.

On Oracle, `DATABASE_NAME` is the service (`FREEPDB1`, say), and `DATABASE_DEFAULT_GRAPH_PATH` names the graph,
`drivine` by default. A graph is drivine4j's tables, `<graph>_node`, `<graph>_edge` and `<graph>_schema`, and a SQL
property graph over them, made on the first connection when they aren't there, so one store serves the JVM, Node and
Python. `<graph>_schema` records the indexes and constraints Drivine made as drivine4j records them, so each library
sees and drops the indexes the others made, and makes none beside them. A range index is two indexes, as drivine4j
makes it: one over the property whatever the label, and one over the label's nodes alone, which a filter is read
through. A uniqueness constraint comes with a range index over its properties, which stays when the constraint is
dropped. The object manager writes SQL for it, and the query DSL, grouped reads, search and schema management work as on
the graph engines. A raw `where` is SQL over the node's row, `n`: its `labels`, `props` (a JSON object keyed by stored
name) and `stamp`, say `JSON_VALUE(n.props, '$.name') = $name`. A property of the node may be written as Cypher writes
it, `n.name = $name` (or ``n.`a name` ``), and is read as that property; a property compared with `true` or `false` is
compared with the text a JSON boolean is read as. Your own statements are SQL too, unless written as
Cypher (see [Cypher on Postgres and Oracle](#cypher-on-postgres-and-oracle)). `matches` takes a regular expression in
Oracle's POSIX dialect, matched against the whole value. A leading `(?i)` ignores case; what Java adds, such as
`(?:…)`, a look-around or `\b`, is not read. A backreference names the group the expression numbers it by,
`\1` to `\8`; `\9` is refused, as the group that anchors the expression would make it the tenth, which POSIX does not
have. A vector index is a table of embeddings beside the nodes, holding those of the index's size: a node written with
another size is left out, not refused. `load_nearest` is approximate through it. With no index made, or one of another
size than the vector's, it measures every node of the label whose embedding is of the vector's size, and is exact.

On Postgres, `DATABASE_NAME` is the database, and `DATABASE_DEFAULT_GRAPH_PATH` names the graph, `drivine` by default:
see [Postgres](#postgres).

Or in code:

```python
registry.builder().database_type(DatabaseType.MEMGRAPH).host("localhost").register("graph")
```

### Postgres

Postgres 14 or later (tested on 16), as drivine4j states, keeps a graph in two tables, as Oracle does, read with joins
and written through a function the store keeps. No extension is needed but pgvector, for vector search, which Drivine
makes where the server has it. Models, the query DSL and the object manager are the same as on the graph engines; the
persistence manager runs plain SQL, with cursors and transactions.

```
DATABASE_TYPE=POSTGRES
DATABASE_HOST=localhost
DATABASE_PORT=5432
DATABASE_NAME=app
DATABASE_USER=app
DATABASE_PASSWORD=…
DATABASE_DEFAULT_GRAPH_PATH=drivine   # names the graph's tables; this is the default
```

The graph's name names tables, so it is letters, digits and underscores, begins with a letter, and is at most 49
characters; Postgres folds it to lower case. The tables are made when an object manager first runs a statement.
Postgres is a graph store like the others, so a schema catalog that names no database applies to it too, and makes its
tables at startup: to keep a database you use only for plain SQL as it is, narrow the catalog with `.for_database(...)`.

**How a graph is stored.** drivine4j's store, byte for byte, so the JVM, Node and Python read and write one graph:
`<graph>_node (nid, labels, props, stamp)` and `<graph>_edge (eid, src, dst, type, props)`, with `labels` and `props`
as `jsonb` and a GIN index over the labels, so a node carries any number of them; `<graph>_schema`, the indexes and
constraints Drivine made; and `<graph>_write`, the function a save's steps are carried out by, as one statement that is
applied whole or not at all.

- A statement you write yourself is SQL, unless written as Cypher (see
  [Cypher on Postgres and Oracle](#cypher-on-postgres-and-oracle)). Parameters are named `$name`, as in Cypher, and a
  statement is bound only the ones it names. A row comes back as a dict of its columns, a `jsonb` column as Python
  values.
- A raw `where`, for a load or a delete, is SQL over the root's row: its `props`, `labels` and `stamp`, say
  `(n.props ->> 'name') = $name`. A property of the root may be written as Cypher writes it, `n.name = $name` (or
  ``n.`a name` ``), and is read as that property, as text; a property compared with `true` or `false` is compared with
  the text a JSON boolean is read as.
- A number is compared as a number only where one is stored: `'7'` is not `7`, as on the graph engines.
- Text is ordered by the database's collation, so a page of mixed-case text may not be in the order Neo4j gives
  (`a, b, B` rather than `B, a, b`). The range indexes are made in that collation, so an order and a seek are read
  through them.
- A save checks the stamp, as on the graph engines. A save turned away by a serialization failure (`40001`) or a
  deadlock (`40P01`) is run again.
- Index the id of each class you save: a range index or a uniqueness constraint is a partial index over the label's
  nodes, which finds a node by its id.
- `matches` takes a Postgres regular expression, matched against the whole value.
- A vector index is a pgvector HNSW index over the embeddings of the label's nodes of its size; `load_nearest` is
  approximate through it, and exact with no index made. A full-text index is a trigram index over the text a search
  reads, which narrows the search; `load_matching` scores the same with it and without it, and reads words, a
  `"phrase"`, `+must`, `-not` and a `prefix*`.

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

### Cypher on Postgres and Oracle

A statement written as Cypher, with `cypher_statement` or `cypher_file`, is compiled to SQL over the graph's tables on
Postgres and Oracle, and gives the rows a graph engine gives, as drivine4j compiles it:

```python
from drivine import QuerySpecification, cypher_statement

spec = QuerySpecification(cypher_statement("MATCH (p:Person {city: $city}) RETURN properties(p)"))
people = await pm.query(spec.bind({"city": "Sydney"}))
```

- A read is a query. A write (`CREATE`, `MERGE`, `SET`, `REMOVE`, `DELETE`) is an anonymous PL/SQL block on Oracle, and
  a function made for the statement in `pg_temp` on Postgres: one statement, applied whole or not at all. A block hands
  its rows back whole, so on Oracle a write is run with `query`, and a cursor over one is refused. On Postgres a cursor
  over a write writes when it is opened, as on a graph engine.
- Parameters are named, `$name`: a statement bound to a list of them is refused.
- A row of one item is its value, and a row of several a list of them. A node is its properties.
- A value is compared as JSON, so `'7'` is not `7`, and a missing property is null. A map keeps its keys in the order
  written.
- A map or a list that holds a node or a relationship (`{x: r}`, `[r, 1]`) compares by what it holds, not by its
  properties, through a `WITH` or a `CALL { … }` too, as on a graph engine. Unwound by `UNWIND`, as a comprehension's
  or a reduce's variable, or carried past a write in the statement, it compares by its properties.
- openCypher's temporal values are their ISO 8601 text, as the store keeps a moment: `date`, `datetime`, `duration` and
  the rest make one, their fields read, and two moments compare as the moments they are. A parameter of a `date`,
  `time`, `datetime` or `timedelta` is a temporal value of its kind.
- `UNION`, `CALL { … }`, `EXISTS { … }`, `COUNT { … }`, `FOREACH`, `=~`, variable-length relationships, `shortestPath`
  and `allShortestPaths` compile, as do quantified relationships (`-[:R]->{1,3}`) and quantified path patterns
  (`(a) ((x)-[r:R]->(y) WHERE x.v < y.v){1,3} (b)`), whose variables are lists of what each repetition matched. On
  Oracle, whose recursive queries read nothing of the row they are in, a quantified path pattern's `WHERE` and
  properties read no variable from outside it, and hold a subquery only where no variable of its `MATCH` or before it
  is named, and that walks no variable-length relationship or quantified path pattern of its own; on Postgres they may.
  What isn't compiled yet (a path returned whole, `SKIP` of an expression…) is
  refused by name before anything is sent, and what isn't Cypher with where it went wrong.
- `CALL db.labels()` and `CALL db.relationshipTypes()` compile, alone or within a query with `YIELD`, and give each
  label and type something has now, read from the nodes and relationships; any other procedure is refused. A parameter
  may be named by a keyword (`$limit`), as in Neo4j. Functions of numbers (`exp`, `log`, `sin`, `atan2`, `round` by a
  mode …) give the floats Neo4j gives, but where Neo4j gives NaN or an infinity (`log(0)`, `sqrt(-1)`, `exp(1000)`) the
  statement fails: JSON holds neither. A float is written as Java writes a double, by `toString` and added to a text:
  `1.0E7`, `1.2E-4`.
- On Postgres, compiled Cypher works float arithmetic out in exact decimal (`numeric`), not as a double, so a result
  can differ from a graph engine's in its last digits: `0.1 * 3` is `0.3`, where Neo4j gives `0.30000000000000004`. A
  float's text carries no trailing zeros: `toString(4.0 / 2)` is `"2.0"`.
- On Oracle a text read out of JSON is at most 4,000 bytes, and an empty one is null; `reduce` doesn't run in a
  statement that writes, in the step of a `reduce`, or where its step reads a path; and `map[key]` reads null where an
  expression gives the key.
- What each statement gives is the [Cypher spec](https://github.com/liberation-data/drivine-cypher-spec), which the
  three Drivine libraries run alike.

The parser runs on ANTLR's runtime, which `drivine[postgres]` and `drivine[oracle]` install.

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

### FalkorDB

FalkorDB has no multi-statement transactions: each statement commits as it runs. With `DATABASE_TRANSACTION_MODE=WARN`,
the default, a transaction runs statement by statement and a rollback logs that nothing was rolled back; a `save_all`
of several statements does the same. With `STRICT`, a transaction is refused at its first statement, and such a batch
raises `NonAtomicBatchError`, so no code path can rely on atomicity it doesn't have.

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
leads = await gom.load_all(PersonCareer, "n.bio CONTAINS $text", {"text": "Lead"})   # Cypher over the root, n
total = await gom.count(Person)
```

### Queries

A query is built from a class's **references**, which mirror its fields:

```python
from drivine import any_of, not_, refs

q = refs(PersonCareer)
leads = await gom.load_all(
    PersonCareer,
    where=q.person.bio.contains("Lead"),
    order_by=q.person.name.asc(),
    limit=20,
)

await gom.load_all(PersonCareer, where=[                                 # a list: all must hold
    q.employment_history.target.name.eq("Acme"),                         # across a relationship
    any_of(q.person.name.eq("Ada"), q.person.name.eq("Grace")),
    not_(q.person.bio.is_null()),
])

# one relationship that meets both, rather than each met by any
q.employment_history.any(lambda it: [it.role.eq("Engineer"), it.target.name.eq("Acme")])
q.employment_history.none(lambda it: it.role.eq("Lead"))
```

- **Operators:** `eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `in_`, `not_in`, `contains`, `starts_with`, `ends_with`,
  `matches`, `contains_ignore_case`, `eq_ignore_case`, `has_item`, `is_null`, `is_not_null`. On a node:
  `instance_of(Type)`, `has_any_label(...)`, and `property(name)` or `predicate(name, operator, value)` for a property
  named at run time by its stored name.
- **Fields go by their Python names**, `q.start_date`, and Drivine writes the stored names. A field the class doesn't
  have fails as the query is written, naming it and the class. `q.x == 1` raises: write `q.x.eq(1)`.
- **Order and pages:** `order_by` takes terms, `asc()` or `desc()`, then `nulls_first()` or `nulls_last()`; `limit`
  and `skip` apply in the database. An order on a list relationship's property sorts that list in each object.
- **Keyset pages:** `seek=[q.released.after(last.released), q.uuid.after(last.uuid)]`, one value for each term of
  `order_by`, ending with a unique key.
- **An order by an expression:** `plus`, `minus`, `times`, `div`, `negate`, `exp`, `floor`, `coalesce`, `case_of`
  with `above`, `below`, `at_least`, `at_most`, `equal_to`, `has_value` and the rest, all computed in the database.
- `count`, `delete`, `delete_all` and `patch` take conditions too.

### Typed references

`refs` is untyped: Python can't derive a reference type for each field from a class, as TypeScript does. A
misspelt field still fails, but when the query is written. To have pyright or mypy check each query, and an editor
complete its fields, write typed references for your models:

```
python -m drivine.stubs myapp.models                 # writes myapp/models_refs.py and models_refs.pyi
python -m drivine.stubs myapp.models --check         # in CI: fails when the models changed since
```

Then import `refs` from the module written:

```python
from myapp.models_refs import refs

q = refs(PersonCareer)                  # PersonCareerRefs
q.person.bio.contains("Lead")           # StringRef
q.person.born.gt("1815")                # error: born is ComparableRef[int]
q.person.nmae                           # error: PersonNodes has no attribute nmae
```

Each field's reference follows from its type: text is a `StringRef`; numbers, dates and times are a
`ComparableRef`; booleans and enums a `ValueRef`; lists and sets a `ListRef`; a property bag gives any entry; and a
relationship leads to the fields of the nodes it reaches, with `any` and `none`. At run time the module is drivine's
`refs`, so nothing changes but what the checker knows. Several modules can share one: `python -m drivine.stubs
myapp.models myapp.billing --out myapp/query_refs.py`.

### The model file

The same step writes `.drivine/model.json` beside your `pyproject.toml`: your fragments, relationship fragments and
views, and how each is stored, in the [format](https://github.com/liberation-data/drivine-cypher-spec/tree/main/model)
drivine4j and Drivine (TypeScript) write too. [Drivine Studio](https://github.com/liberation-data/drivine-studio)
reads it to browse your model and load it, with the Cypher a load runs, without your application running.

It is read from the models ModelRegistry builds of your classes, so it says what a load does: each class's id is its
module and name (`myapp.models.Person`), each field's stored name is the one a save writes (`created_at` as
`createdAt`), and its type is read from its annotation. A class the registry refuses is named, and left out.

Keep the file in source control, as you would a lock file: a change to the model shows in review, and `--check` in CI
fails when it is stale. A class you remove stays in the file, marked `"removed": true`, as the store may still hold
its nodes; delete the entry by hand once it doesn't. The file is written only when it changes. `--no-model`, or
`model = false` under `[tool.drivine]` in `pyproject.toml`, writes none; a module in no project, with no
`pyproject.toml` above it, gets none either.

The sample's file is [`sample/.drivine/model.json`](sample/.drivine/model.json).

### Grouped reads

Group a fragment's nodes by keys, with numbers about each group, in one statement:

```python
from drivine import avg, count, hop, key, path_key, refs

q = refs(Review)
top = await gom.groups.load(
    Review,
    keys={"page": key(q.page)},                                  # or path_key(Page, [hop("REVIEWS")]): the node
    aggregates={"votes": count(q.helpful.eq(True)), "rating": avg(q.rating)},
    where=q.published.is_not_null(),
    having=[("votes", "gte", 3)],
    order_by=[("votes", "desc")],
    limit=10,
)
# [{"page": "/pricing", "votes": 12, "rating": 4.5}, ...]

await gom.groups.count_by(Review, "page")                       # {"/pricing": 14, ...}
```

Aggregates are `count`, `sum_`, `avg`, `min_` and `max_`, each with an optional filter. Ties in an order are broken by
the keys, so a page of groups is the same every time.

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
- **A set field** is stored as a list of its items, sorted where they order (as the set iterates where they don't), and
  a `JsonPacked` one packed so: a set is written alike in every process, whatever order it iterates in. `patch` writes
  one so too, and `edges.relate` writes a set property so. One that 0.1.0 wrote, in the order it iterated in, loads and
  `update` saves it, but until a save writes it again a `where` that compares the whole set, which it writes sorted,
  doesn't find it on the graph engines (on Postgres and Oracle a whole-set `where` on a set that isn't `JsonPacked`
  doesn't find it either way), and on Neo4j a save given `from_` of a `JsonPacked` one is refused.
- **A flat `PropertyBag(flat=True)`** stores each entry under its bare key and loads every property no field or other
  bag is stored under. A save refuses, before it writes anything, an entry whose key a field (the id among them) is
  stored under, one under another bag's prefix, or one beginning `__drivine.`, Drivine's own, such as the stamp.

The statements are TypeScript's, character for character: the tests check them against those TypeScript's Drivine
writes for the same models.

### Saving

```python
saved = await gom.save(career)                                       # fields that aren't None; relationships added
await gom.save(career, replace=["employment_history"])               # this list is the whole list
await gom.save(person, only=["name"])                                # or except_=["embedding"]
await gom.save(person, null_policy=NullPolicy.CLEAR)                 # a field assigned None clears its property
await gom.save(edited, from_=loaded)                                 # checked against the object as it was loaded
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
- **On Neo4j a load can be handed a stamp newer than its data.** Neo4j reads under read committed, without a snapshot,
  so a load that runs while another writer commits can find that writer's stamp and the data from before its write.
  `update` therefore compares each field of the root as it loaded it too, under the node's lock, and loads again if
  one differs: a field's value (a set field's items, in any order and each once or more, and a `JsonPacked` field's as
  the text the node held), a `PropertyBag`'s keys and their values, and the labels a `NodeLabels` field reads. A
  `save` checks the same when you give it the object as loaded, as `from_`. Without `from_` it has only the stamp to
  go on, and in that moment it can write over the other writer's change: hold on to what you loaded and pass it, or
  change the node with `update`. Where only the stamp makes the round trip, as through a web form, the stamp is all
  there is. Memgraph reads from a snapshot, FalkorDB runs no read beside a write, and Oracle and Postgres read a
  statement whole, so on them the stamp is what was read.
- **`from_` is the object as you loaded it**, when what you save is a changed copy of it. The save is then checked
  against the stamp `from_` carries, whatever stamp the copy carries, as `update` checks against what it loaded, and on
  Neo4j against each field of `from_`'s root too. A `JsonPacked` field is compared as a save packs it: `from_` holds the
  value, not the text the node held, so where another writer packed the same value with other spacing the save is
  refused, and `update`, which compares the text, is the way to change the node. A `from_` of another class, with
  another id, or with no stamp is refused before anything is written, and so is the object saved itself. A copy may
  share an unchanged root with `from_`, as a view copied to change only a relationship does; change the root through a
  copy (`model_copy`), not in place.

  ```python
  loaded = await gom.load(Person, uuid)
  # ... later, from what the user changed:
  await gom.save(loaded.model_copy(update={"email": form.email}), from_=loaded)
  ```
- **`save_all`** runs its statements in one transaction, or in yours, so a failure leaves nothing written. It writes
  the fragments' classes in the order of their names and the fragments of each in the order of their ids, so two
  batches over the same nodes take their locks in the same order. The objects come back in the order given.
- **`patch`** writes named fields of one node by id, if it meets a condition, without loading it, and says whether it
  found the node, applied the patch and changed anything. It never makes a node.
- **Edges** relate and unrelate stored nodes by id, marking both ends' stamps.
- **A cascading delete** of a view follows the relationships a save of it writes, through nested views. It leaves what
  a read-only field loads (a `ReadOnly()` field, a `GraphPath`, and a list of fragments read over several hops): other
  code writes those nodes. `DELETE_ALL` deletes every node the view reaches, and `DELETE_ORPHAN` each one that nothing
  outside the view still needs. That keeps a node something outside the view refers to, at any level, with everything
  the view holds below it: a bookmarked message keeps its attachments, and a pinned sub-session its messages and
  sub-sessions. The rest goes, the session that held a kept message among it. `DELETE_ORPHAN` isn't available on
  Memgraph. A view that contains itself (`parts: list["SessionTree"]`, with a `max_depth`) deletes what a load of it
  reads, every level and what each holds through the relationships a save writes; one that reaches below its
  `max_depth` is refused with a `DrivineError` before anything is written, saying how deep the view reads: raise the
  `max_depth` to cover the deepest level.
- **Outside a transaction**, a save the engine turns away because another writer held the node is run again after a
  pause. Inside one, it joins the transaction, and fails for its owner to decide.
- A save that lost its connection, or whose authorization expired, is not run again, though the Neo4j driver calls that
  retryable: it may have been applied, and a second run would find its own stamp and report a save that was applied as
  refused.

The store format, stamps included, is TypeScript's and drivine4j's, so the three can share a store.

## Schema

Declare indexes and constraints on the fragments, and hold them on every database at start-up:

```python
class Chunk(NodeFragment, labels=["Chunk"], schema=[RangeIndex(["section_id", "position"])]):
    uuid: Annotated[str, NodeId(), Unique()]
    section_id: str
    position: int
    text: Annotated[str, FullTextIndex()]
    embedding: Annotated[list[float] | None, VectorIndex(similarity="COSINE")] = None


class AppDrivine(DrivineAssembly):
    def schema_options(self) -> SchemaOptions:
        return SchemaOptions([SchemaCatalog.from_fragments(Chunk, dimensions=lambda label, prop: 1536)])

await AssemblyHolder.shared(AppDrivine).start()      # makes what is missing; idempotent
```

`start()` registers the databases whether or not there are catalogs, as drivine4j does, so a missing `DATABASE_TYPE`
raises `DrivineError` there rather than on first use. Without pi-pilgrim, enforce it yourself:
`await SchemaManager(registry, factory, options).enforce()`.

- **`ensure` is idempotent.** An item that is there is left; one there with another shape (a vector index's dimensions,
  say) is reported as DRIFT and left, unless `recreate_on_drift`. A uniqueness constraint the data already breaks is a
  VIOLATION, with a sample of the keys that repeat: each its `properties`, the `count` of nodes that hold it, and
  `nodes`, up to the sample size of them, each named by the engine's id (`elementId`) and, for a constraint read from
  a fragment, its `NodeId` (`uniqueness_constraint(..., node_id_property="uuid")` by hand). This is a breaking change
  from 0.1, where each entry was the repeated value itself: it is now a `ViolatingKey`, the value in its `properties`.
  A sample size of 0 skips reading the data first, and the engine's refusal of a constraint the data breaks is raised.
  FalkorDB doesn't refuse one: it builds it in the background and lists the build as FAILED. Drivine then drops the
  failed constraint, which enforces nothing, and reads the data: a key held more than once is a VIOLATION, as in
  drivine4j, with an empty sample at sample size 0, and a build that fails with no key held more than once raises.
  Once the data is clean, `ensure` makes the constraint. A constraint FalkorDB already lists as FAILED, or still
  UNDER CONSTRUCTION (another process's build, or an earlier run's), is not taken as ALREADY_MATCHING: one under
  construction is waited on, and matches once built, and a failed one is dropped and made again. `drop` also removes a
  constraint FalkorDB failed to build.
  `mode="FAIL_FAST"`, the default, raises on either; `"WARN"` logs.
- **Versions.** `catalog.with_version("2")` recreates the catalog's items once, the first time it is enforced. Items an
  owner applied before and no longer declares are reported, never dropped. Each owner's version and inventory are kept
  in a `_DrivineSchema` node, as drivine4j and TypeScript keep them.
- **Schema known only at runtime**, a vector index whose dimensions come with the embedding model or an index for each
  label a tenant declares, is given to the manager under an owner: `await schema_manager.ensure("dice",
  vector_index("Proposition", "embedding", 1536))`, or `ensure_catalog(catalog)` for a catalog naming its owner and
  databases. The items join what the owner declared, for the life of the manager, and are applied under the same
  options; a later `enforce` or `recreate_all` applies them with the rest. A declaration that conflicts with another
  owner's, or the owner's own, raises, and nothing is kept or applied. The marker records them as ensured at runtime
  (`runtime:` before the key, as drivine4j writes it), so the next process's `enforce` keeps them rather than calling
  them orphans; when it recreates the owner (on a version change, under `recreate_on_startup`, or by `recreate_all`), it
  marks them (`runtime-stale:`), and the next `ensure` recreates them.
  A version change is left to `enforce`.
- **Catalogs are read once**, from the options the manager is built with; a catalog made later isn't seen, and schema
  known later is given to `ensure`. With nothing to apply, no catalog or only empty ones and nothing ensured, the
  manager says so at INFO: that it found no `SchemaCatalog`, or that all it found are empty, naming their owners. So a
  missing catalog, and one left empty until an embedding model exists, can be told from an application with no schema.
- **A vector field needs its `VectorIndex`.** Drivine knows a field is an embedding from the marker alone: without it
  the field is saved as a plain list, not as a vector FalkorDB's index serves, and `load_nearest` can't find the
  index. So a catalog's `vector_index` over a fragment's field without one is refused with a `DrivineError` naming
  the class, the field and the spec, whether the catalog is enforced first or the fragment's model is built first.
  One given to `ensure` is refused the same way, before it is kept or applied.
  A spec beside the marker is allowed (it tunes the index), as is one on a label no fragment maps.
- **An index in the way.** Neo4j refuses a uniqueness constraint while a plain range index covers the same label and
  properties. `ensure` reports that as BLOCKED_BY_INDEX, naming the index, and changes nothing: `FAIL_FAST` raises,
  `WARN` logs. With `replace_blocking_index=True` (on `gom.constraints.ensure`, or in `SchemaOptions`) the data is
  read, and unless a value repeats the index is dropped and the constraint made, its own index taking the plain one's
  place. A constraint given to `schema_manager.ensure` is reported and replaced the same way. Drivine's own keys (the
  schema marker's, the store's) always replace one. On FalkorDB the constraint needs the range index, and Memgraph,
  Oracle and Postgres make it beside one, so none is ever reported in the way there.
- **By hand:** `gom.indexes.ensure(range_index("Session", ["lastActivityAt", "sessionId"]))`, `gom.indexes.recreate`,
  `gom.indexes.drop`, `gom.constraints.ensure(uniqueness_constraint("Person", "email"))`, and `list`.
- **Each engine's DDL** is written for it: named items on Neo4j, Memgraph's text and vector indexes, FalkorDB's index per
  property and its constraint commands, Oracle's tables beside the nodes, and Postgres's partial indexes over a label's
  nodes, recorded in the graph's catalog as drivine4j records them (a pgvector HNSW index for a vector index, a trigram
  index for a full-text one). Neptune has no schema management. Vector index tuning (`hnsw_m`, `hnsw_ef_construction`, and an engine's own
  options) is passed where the engine takes it, and reported where it doesn't.

### Search

A fragment's `VectorIndex` field drives `load_nearest`, and its `FullTextIndex` drives `load_matching`. Either takes a
fragment or a view, searched by its root, and gives each hit with its score, best first:

```python
hits = await gom.load_nearest(Chunk, embedding, top_k=5, where=refs(Chunk).section_id.eq(section))
for hit in hits:
    print(hit.score, hit.value.text)

found = await gom.load_matching(Chunk, "graph AND database", top_k=10, threshold=0.5)
```

- **One scale on every engine.** A nearest score is a similarity, higher nearer: (1 + cos) / 2 for cosine, 1 / (1 + d²)
  for euclidean. A matching score is scaled so that the best hit is 1: the best with every label of the root, as the
  index is over the first.
- **Memgraph's cosine scores are read from its distance**, not its similarity, which is the absolute value of
  1 - distance (memgraph/memgraph#5199). Earlier releases read the similarity, so a vector more than 90 degrees from
  the query scored as its mirror image does: a cosine of -0.6 as 0.8, not 0.2. Those scores, and the hits a cosine
  `threshold` keeps, change; positive cosines and euclidean scores don't.
- **Filters apply after the search:** `threshold`, `where`, and a view's required relationships. So a search can return
  fewer than `top_k`; `search_k` asks the index for more, and `top_k` trims after the filter.
- **A full-text search is bounded where the index yields.** A matching score is scaled over every hit the index
  yields, so on Neo4j, Memgraph and FalkorDB `top_k` bounds the hits too, and an ordinary query that matches most of
  a corpus costs no more than them. The index is asked for the best `top_k` hits. Where something filters them, it is
  asked for `top_k × 10` (`FILTERED_OVER_FETCH`), and the filter keeps the best `top_k` of those. Filters are a
  `where`, a view's required relationships and paths, and a label beyond the first (the index is over the first); a
  threshold keeps the best hits, so it isn't one. So a filter that passes fewer than one in ten of the best hits can
  return fewer than `top_k` rows while the index holds more that pass it. The index is asked for no more than 2,147,483,647 hits, and Memgraph's for no more than
  1,000,000, however large `top_k` is.
- `property` names the field to search when a fragment declares more than one index; `partition_label` searches the
  vector index of another label. Full-text text is the engine's own query language, passed as it is.

### Index advice

An ordered query with no range index over exactly its ordered properties scans and sorts. Drivine says so, once for
each label and properties, naming the index to make. `gom.index_advice = IndexAdvicePolicy.FAIL` raises instead, for
development and CI (or override `index_advice()` on the assembly); `OFF` says nothing. The index list is read once:
`gom.index_advisor.refresh()` after making indexes as the application runs.

### Store identity

`await gom.store_identity()` gives the store's stable id, minted in the store on first sight: two connections see the
same id when, and only when, they look at the same data.

## Sync

`drivine.sync` is the same API without `async` and `await`, for scripts, notebooks, Django and Flask:

```python
from drivine.sync import DatabaseRegistry, PersistenceManagerFactory, StatelessGraphObjectManager, transactional

registry = DatabaseRegistry()
registry.build_or_resolve_from_env()
gom = StatelessGraphObjectManager(PersistenceManagerFactory(registry).get())


@transactional
def promote(uuid: str) -> None:
    person = gom.load(Person, uuid)
    gom.save(person.model_copy(update={"bio": "Lead"}))
```

- **Each driver's own sync client**, not an event loop behind the scenes: it costs nothing extra, and works where a loop
  is already running, as in Jupyter.
- **The same models, query DSL and backends.** A class is one class in both APIs; only what does I/O is sync.
- **Each thread has its own transaction**, as asyncio gives each task's context its own.
- `@transactional` from `drivine.sync` takes a plain function, and drivine's takes an async one.

It is written from the async source by `scripts/unasync.py`, as the Neo4j driver writes its sync API, and tested by the
same tests, written alike.

## With pi-pilgrim

`DrivineAssembly` is a [pi-pilgrim](https://github.com/liberation-data/pi-pilgrim) assembly holding the registry
and the managers, for your own assembly to use. It needs `pip install "drivine[pilgrim]"`:

```python
from pipilgrim import AssemblyHolder, PilgrimAssembly
from drivine.drivine_assembly import DrivineAssembly      # drivine.sync.drivine_assembly for the sync API


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

Nothing else needs pi-pilgrim: `import drivine` doesn't load it, and the managers are plain classes.

## The sample app

[`sample/`](sample) is the port of drivine4j's sample, as TypeScript's Drivine has it: an issue tracker and a guide's
users, with fragments, views, relationship fragments, polymorphic users, property bags, stamps and search, and its
tests on every engine. It is a Poetry project of its own, whose build writes its typed references.

## Testing

`pip install "drivine[testing]"` adds a pytest plugin. With

```toml
[tool.pytest.ini_options]
drivine_test_containers = true
```

it starts a container for each datasource the environment configures, Neo4j, Memgraph, FalkorDB, Postgres (pgvector's
image, with user `drivine`, password `drivine-test` and database `drivine`) or Oracle, before the tests run, and points
the datasource at it. Datasources of one type share a container. `USE_LOCAL_NEO4J=true` (or `_MEMGRAPH`, `_FALKORDB`,
`_POSTGRES`, `_ORACLE`) leaves that type's datasources as they are, to test against your own instance, and
`NEO4J_TEST_IMAGE` (or `MEMGRAPH_TEST_IMAGE`, `FALKORDB_TEST_IMAGE`, ...) picks another image. The plugin's own images
are `neo4j:5.26-community`, `memgraph/memgraph:3.12.0` and `falkordb/falkordb:6.0.0`.

Drivine's own tests run on Oracle only with `ORACLE_TESTS=true`: its image is large, and the database wants two
gigabytes. A run without it ends with the line `Oracle tests skipped: set ORACLE_TESTS=true to run them`. CI runs the
Oracle tests in a job of their own, with `pytest -k oracle`. The tests run on `neo4j:latest`, as the Cypher spec is
written for a current Neo4j, where the plugin's own default is 5.26. They run on Memgraph 3.14.0 and FalkorDB 6.0.3,
the current releases, so that an engine release that changes what Drivine reads or writes shows up soon.

## Logging

Drivine logs through the standard `logging` module: `drivine.statements` logs each statement at DEBUG, with its
session and how long it took. `DRIVINE_LOG_LEVEL`, as TypeScript's Drivine reads it, sets the level of the `drivine`
logger when the application hasn't.

## Building

```
uv sync
uv run pytest            # starts Neo4j, Memgraph, FalkorDB and Postgres in Docker
ORACLE_TESTS=true uv run pytest -k oracle   # and Oracle, for its tests alone
uv run ruff check . && uv run pyright
```

`drivine/sync` and `tests/sync_integration` are written from the async source: change that, and run
`uv run python scripts/unasync.py`. A unit test fails while they are out of date. `drivine/sync/concurrency.py`, the
counterpart of `drivine/concurrency.py`, is the one sync module written by hand, and a test marked `async_only` (one
that runs tasks concurrently) is left out of the sync tests.

The integration tests run on Neo4j, Memgraph, FalkorDB, Postgres and Oracle. A test marked `written_in_cypher` seeds
and reads back in Cypher with statements of no language, and on Postgres and Oracle those are sent as Cypher, which is
compiled to SQL (`tests/integration/sql_cypher_fixtures.py`, as drivine4j's `SqlCypherFixtures`). A test marked
`graph_engine_only` runs only on the graph engines, for the reason it gives, and one marked `fails_on_sql_stores` is
expected to fail on Postgres and Oracle until the issue it names is fixed.

## License

Apache 2.0.
