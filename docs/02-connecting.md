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

async with DatabaseRegistry() as registry:          # closes the drivers and pools on the way out
    registry.build_or_resolve_from_env()              # "default"
    registry.build_or_resolve_from_env("analytics")   # ANALYTICS_DATABASE_*

    pm = PersistenceManagerFactory(registry).get()    # the default database
    ...
```

`DrivineAssembly` registers the default database alone; to register others, subclass it and override
`register_databases(registry)` (see [With pi-pilgrim](09-with-pi-pilgrim.md#with-pi-pilgrim)).

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
Cypher (see [Cypher on Postgres, Oracle and SQLite](04-statements.md#cypher-on-postgres-oracle-and-sqlite)). `matches` takes a regular expression in
Oracle's POSIX dialect, matched against the whole value. A leading `(?i)` ignores case; what Java adds, such as
`(?:…)`, a look-around or `\b`, is not read. A backreference names the group the expression numbers it by,
`\1` to `\8`; `\9` is refused, as the group that anchors the expression would make it the tenth, which POSIX does not
have. A vector index is a table of embeddings beside the nodes, holding those of the index's size: a node written with
another size is left out, not refused. `load_nearest` is approximate through it. With no index made, or one of another
size than the vector's, it measures every node of the label whose embedding is of the vector's size, and is exact.

Each session of Drivine's Oracle pool makes no adaptive plan (`ALTER SESSION SET OPTIMIZER_ADAPTIVE_PLANS = FALSE`, run
as the pool opens it; a session it fails in is dropped from the pool, not given out). Once a graph's tables had grown
and shrunk without statistics, Oracle's adaptive join plan gave no rows for a read that filters a node by a label and a
property, where the same plan, fixed, gave them all ([#146](https://github.com/liberation-data/drivine-python/issues/146)).
A statement of yours that relies on an adaptive plan can ask for one with the hint
`/*+ OPT_PARAM('optimizer_adaptive_plans' 'true') */`. To have the sessions left as Oracle opens them, register a
provider made in code with `sets_no_adaptive_plans=False`: `registry.register(OracleConnectionProvider("default",
"localhost:1521/FREEPDB1", user, password, sets_no_adaptive_plans=False))`, its class from
`drivine.connection.oracle.oracle_connection_provider` (`drivine.sync.connection.oracle.oracle_connection_provider` for
the sync API). It is on by default because, without it, a read can come back with no rows and no
error; if you turn it off, run that statement in each session yourself.

Each session of the pool keeps the statements it runs prepared, 20 of them (python-oracledb's `stmtcachesize`), so a
save, the same block each time, is neither sent nor parsed again. A Cypher statement compiled to SQL is not kept: Oracle
holds a kept statement's memory for as long as the driver keeps it, from about 4 MB for a compiled statement to over
20 MB for one that reads a long text, and a few dozen of them ran the instance out of memory (ORA-04036,
[#218](https://github.com/liberation-data/drivine-python/issues/218)). So it is prepared again each time it runs: about
0.3 ms more for a short statement (Oracle reuses its plan; it doesn't parse it again from scratch). The object manager's
statements are kept, so where the object manager does what you need, it is the faster way to Oracle.

Beside the tables an Oracle graph is made with three functions: `<graph>_nulls_equal`, by which a compiled Cypher
statement compares two lists or maps that may hold a null (see
[Cypher on Postgres, Oracle and SQLite](04-statements.md#cypher-on-postgres-oracle-and-sqlite)), and `<graph>_era` and
`<graph>_era_date`, by which it renumbers a year before 1 between Cypher's numbering and Oracle's as it reads and writes
a date or a moment. drivine4j and Drivine for TypeScript make them byte for byte alike. Making them takes
`CREATE PROCEDURE`, which the `RESOURCE` role grants, as well as the `CREATE TABLE` the tables take. A store an earlier
version made gains those it lacks the next time a pool first connects to the graph. Where the user that makes the graph
may not make a function, the graph is made without them, and Drivine logs a warning once for the graph when a statement
would call one, and looks for it again a minute later. Until then, without `<graph>_nulls_equal`, a statement that
writes, or the step of a `reduce`, compares two lists or maps that hold a null and differ as JSON as false, where Cypher
may give null (`a = b`, of `[null] AS a, [1] AS b`), and a read declares the function itself. Without the era
functions, a statement gives the same rows, renumbering a year inline, but Oracle takes longer to plan one of temporal
arithmetic: seconds, and over a minute for a moment two `CALL`s deep.

On Postgres, `DATABASE_NAME` is the database, and `DATABASE_DEFAULT_GRAPH_PATH` names the graph, `drivine` by default:
see [Postgres](#postgres). On either, Cypher you write is compiled to SQL: see
[Cypher on a relational database](03-cypher-on-a-relational-database.md#cypher-on-a-relational-database).

Or in code:

```python
registry.builder().database_type(DatabaseType.MEMGRAPH).host("localhost").register("graph")
```

### Postgres

Postgres 14 or later (tested on 16), as drivine4j states, keeps a graph in two tables, as Oracle does, read with joins
and written through a function the store keeps. No extension is needed but pgvector, for vector search, which Drivine
makes where the server has it. Models, the query DSL and the object manager are the same as on the graph engines; the
persistence manager runs plain SQL, with cursors and transactions, and Cypher compiled to SQL: see
[Cypher on a relational database](03-cypher-on-a-relational-database.md#cypher-on-a-relational-database).

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
characters; Postgres folds it to lower case. The tables are made on the first connection when they aren't there, as on
Oracle, whatever is sent first: Cypher or SQL through the persistence manager, a statement of the object manager, or a
schema change. A user who may read the tables but not make them works once they are made. Postgres is a graph store
like the others, so a schema catalog that names no database applies to it too, and makes its indexes at startup.

**How a graph is stored.** drivine4j's store, byte for byte, so the JVM, Node and Python read and write one graph:
`<graph>_node (nid, labels, props, stamp)` and `<graph>_edge (eid, src, dst, type, props)`, with `labels` and `props`
as `jsonb` and a GIN index over the labels, so a node carries any number of them; `<graph>_schema`, the indexes and
constraints Drivine made; and `<graph>_write`, the function a save's steps are carried out by, as one statement that is
applied whole or not at all.

- A statement you write yourself is SQL, unless written as Cypher (see
  [Cypher on Postgres, Oracle and SQLite](04-statements.md#cypher-on-postgres-oracle-and-sqlite)). Parameters are named `$name`, as in Cypher, and a
  statement is bound only the ones it names. A row comes back as a dict of its columns, a `jsonb` column as Python
  values.
- A raw `where`, for a load or a delete, is SQL over the root's row: its `props`, `labels` and `stamp`, say
  `(n.props ->> 'name') = $name`. A property of the root may be written as Cypher writes it, `n.name = $name` (or
  ``n.`a name` ``), and is read as that property, as text; a property compared with `true` or `false` is compared with
  the text a JSON boolean is read as.
- A number is compared as a number only where one is stored: `'7'` is not `7`, as on the graph engines.
- Text is ordered by the database's collation, so a page of mixed-case text may not be in the order Neo4j gives
  (`a, b, B` rather than `B, a, b`). The range indexes are made in that collation, so an order and a seek are read
  through them. Compiled Cypher orders text by its code points instead
  ([Cypher on Postgres, Oracle and SQLite](04-statements.md#cypher-on-postgres-oracle-and-sqlite)).
- A save checks the stamp, as on the graph engines. A save turned away by a serialization failure (`40001`) or a
  deadlock (`40P01`) is run again.
- Index the id of each class you save: a range index or a uniqueness constraint is a partial index over the label's
  nodes, which finds a node by its id.
- `matches` takes a Postgres regular expression, matched against the whole value.
- A vector index is a pgvector HNSW index over the embeddings of the label's nodes of its size; `load_nearest` is
  approximate through it, and exact with no index made. A full-text index is a trigram index over the text a search
  reads, which narrows the search; `load_matching` scores the same with it and without it, and reads words, a
  `"phrase"`, `+must`, `-not` and a `prefix*`. It scores by BM25, as Lucene and so Neo4j's full-text index do (`k1`
  1.2, `b` 0.75): a term weighs as it is rare among the label's nodes, and a text's counts against its length, in
  characters, beside the label's average. A term written again at once, `kiwi kiwi kiwi`, is counted each time. A
  `prefix*` adds the same to every node that holds it, however often and however rare, as Lucene's prefix query does:
  in `kiw* melon`, a node with `kiwi` and no `melon` scores about 0.54 of one with both, as on Neo4j. Oracle without
  Oracle Text scores the same way.

### SQLite

SQLite keeps a graph in a file, with no server: drivine4j's store, in the same tables as Postgres, so a file one library
wrote, drivine4j, Drivine for TypeScript or this one, the others read and write as it is. It takes SQLite 3.45 or later
through Python's own `sqlite3` module, so there is nothing to install; where Python's SQLite is older, install
`pysqlite3-binary` and Drivine uses it. Models, the query DSL and the object manager are the same as on the other
stores; the persistence manager runs plain SQL, with cursors and transactions, and Cypher you write is compiled to
SQLite's SQL, as on Postgres and Oracle (`pip install "drivine[sqlite]"` adds the runtime its compiler takes): see
[Cypher on Postgres, Oracle and SQLite](04-statements.md#cypher-on-postgres-oracle-and-sqlite). Compiled Cypher wants
SQLite 3.53 or later, writing a float as text to 17 significant digits as 3.53 does by default: on an older one, or one
built to write 15, a float may lose its last digits and a `CALL { … }` that groups by a variable it imports is refused
before 3.53, and Drivine logs a warning once. Where Python's SQLite isn't such a one and `pysqlite3`'s is, Drivine uses
`pysqlite3` for the store, plain SQL and the object manager too.

```
DATABASE_TYPE=SQLITE
DATABASE_NAME=/var/lib/app/graph.db   # the file's path: there is no host, port, user or password
DATABASE_DEFAULT_GRAPH_PATH=drivine   # names the graph's tables; this is the default
```

Or in code, `registry.builder().database_type(DatabaseType.SQLITE).database_name("graph.db").register("graph")`. The
file is made if it isn't there, and the graph's tables on the first connection, as on Postgres. `:memory:` is refused:
each pooled connection would open a database of its own. In the async API each connection's calls run on a thread of
the connection's own, so one waiting for the file's lock holds up neither the event loop nor another connection.

**How a graph is stored.** `<graph>_node (nid INTEGER PRIMARY KEY, labels TEXT, props TEXT, stamp TEXT)`,
`<graph>_edge (eid INTEGER PRIMARY KEY, src, dst REFERENCES <graph>_node ON DELETE CASCADE, type, props TEXT)` and
`<graph>_schema`, the indexes and constraints Drivine made. Labels and properties are JSON text, not SQLite's binary
`jsonb`, and labels are written sorted, with nothing escaped that JSON does not require: a label is found by its text.
The stamp is a column. Moments compare and order to the nanosecond, whatever their offset.

- A save is the plan of steps Postgres's write function carries out, carried out by a function Drivine registers on
  each connection, `drivine_write`, under a savepoint: applied whole or not at all, in a transaction or not. The other
  functions a statement calls are registered too: `drivine_delete`, `drivine_epoch`, `drivine_lower` (Unicode's lower
  case, where SQLite's lowers ASCII), `drivine_similarity`, `drivine_regexp_count` and `REGEXP`. A statement of yours
  may call them; a connection you open yourself has none of them.
- A statement Drivine writes that gives back documents opens with `/* drivine:json */`, and its text values are read
  as JSON. One you write is read as it is: a row is a dict of its columns, and `labels` and `props` are text.
- A raw `where` is SQL over the root's row, as on Postgres; a property written as Cypher writes it, `n.name`, is read
  as that property, as text.
- A range index or a uniqueness constraint is a partial index over the property as a statement reads it, for the
  label's nodes. A vector or a full-text index is recorded and nothing is made for it: `load_nearest` and
  `load_matching` measure every node of the label, and `load_matching` scores by BM25, as Postgres does.
- `matches` takes a regular expression in Java's dialect, as on drivine4j, matched against the whole value: a
  `REGEXP` reads it as Java does, and refuses what Python can't read so, such as a script (`\p{IsLatin}`) or a
  look-behind of more than one length.

**What a transaction gives.** Each connection writes ahead to a log, enforces foreign keys and waits for a locked file
for up to its busy timeout, 30 seconds by default.

- There is one writer at a time. A transaction takes the file's write lock as it begins (`BEGIN IMMEDIATE`), so even a
  transaction that only reads waits for the lock, for up to the busy timeout. One that holds it longer than 5 seconds
  is logged as a warning when it ends, committed, rolled back or released.
- Reads outside a transaction don't wait for a writer: each reads the file as it last was.
- Saves and deletes outside a transaction are run again when SQLite turns them away as contended (`SQLITE_BUSY`).
- Each statement Drivine writes applies whole or not at all.
- A connection you open yourself, with `sqlite3.connect`, has none of this: no log, no foreign keys, no busy timeout
  and none of Drivine's functions.

Drivine reads `os.environ`, and doesn't load `.env` files itself: use python-dotenv, pydantic-settings, or your
platform's way.
