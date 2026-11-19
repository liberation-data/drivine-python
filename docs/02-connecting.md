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
name) and `stamp`, say `JSON_VALUE(n.props, '$.name') = $name`, read over the row a statement calls it by, `n_` (the row
the shared module's fragments read). A property of the node may be written as Cypher writes it, `n.name = $name` (or
``n.`a name` ``), and is read as that property, one whose stored name begins with `$`, ``n.`$score` ``, by the name the
store holds it by; a property compared with `true` or `false`, or with a parameter bound to `True` or `False`, is
compared with the text a JSON boolean is read as, and to compare one with a number, read it as a number in SQL:
``CAST(n.`$score` AS NUMERIC) > $least``. Your own statements are SQL too, unless written as Cypher (see [Cypher on
Postgres, Oracle and SQLite](04-statements.md#cypher-on-postgres-oracle-and-sqlite)), and a `$name` in quoted text or a
comment, such as the JSON path `'$."$t"'`, is no parameter. The query DSL's conditions, orders, seeks and grouped reads
are the shared module's SQL for the Cypher the DSL writes for Neo4j
([drivine-cypher-spec#235](https://github.com/liberation-data/drivine-cypher-spec/issues/235)), as in compiled Cypher: a
date is compared by its moment, text is compared and ordered by its code points (`B, a, b`), and an equality of a
property with a range index or a uniqueness constraint is read through layout 2's index of the label. Each statement
carries the optimizer hints that SQL asks for. `matches` takes a regular expression as compiled Cypher's `=~` does,
matched against the whole value, in Oracle's POSIX dialect. A leading `(?i)` ignores case. A backreference is read as
Java reads it only where it is `\1` to `\8`, to a simple group; the shared module refuses any other, with why. A vector
index is a table of embeddings beside the nodes, holding those of the index's size: a node written with another size is
left out, not refused. `load_nearest` is approximate through it. With no index made, or one of another size than the
vector's, it measures every node of the label whose embedding is of the vector's size, and is exact.

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

Before that, each session runs the statement the shared module states for it, `ALTER SESSION SET NLS_COMP = BINARY
NLS_SORT = BINARY`, so compiled Cypher compares, orders, groups and finds text by its code points whatever `NLS_COMP` and
`NLS_SORT` the database or a logon trigger gave the session
([#566](https://github.com/liberation-data/drivine-python/issues/566)). Under a linguistic sort such as `BINARY_CI`,
`'B' < 'a'` would be false and `'a' = 'A'` true, with no error. `sets_no_adaptive_plans=False` leaves this one in place.

Each session of the pool keeps the statements it runs prepared, 20 of them (python-oracledb's `stmtcachesize`), so a
save, the same block each time, is neither sent nor parsed again. A Cypher statement compiled to SQL is not kept: Oracle
holds a kept statement's memory for as long as the driver keeps it, from about 4 MB for a compiled statement to over
20 MB for one that reads a long text, and a few dozen of them ran the instance out of memory (ORA-04036,
[#218](https://github.com/liberation-data/drivine-python/issues/218)). So it is prepared again each time it runs: about
0.3 ms more for a short statement (Oracle reuses its plan; it doesn't parse it again from scratch). The object manager's
statements are kept, so where the object manager does what you need, it is the faster way to Oracle.

Beside the tables an Oracle graph is made with functions: `<graph>_nulls_equal`, by which a compiled Cypher statement
compares two lists or maps that may hold a null (see
[Cypher on Postgres, Oracle and SQLite](04-statements.md#cypher-on-postgres-oracle-and-sqlite)), `<graph>_era` and
`<graph>_era_date`, by which it renumbers a year before 1 between Cypher's numbering and Oracle's as it reads and writes
a date or a moment, and `<graph>_temporal_arith`, which works out `+`, `-`, `*` and `/` of a temporal value read from
the store. drivine4j and Drivine for TypeScript make them byte for byte alike. Making them takes
`CREATE PROCEDURE`, which the `RESOURCE` role grants, as well as the `CREATE TABLE` the tables take. A store an earlier
version made gains those it lacks the next time a pool first connects to the graph. Where the user that makes the graph
may not make a function, the graph is made without them, and Drivine logs a warning once for the graph when a statement
would call one, and looks for it again a minute later. Until then, without `<graph>_nulls_equal`, a statement that
writes, or the step of a `reduce`, compares two lists or maps that hold a null and differ as JSON as false, where Cypher
may give null (`a = b`, of `[null] AS a, [1] AS b`), and a read declares the function itself. Without the era
functions, a statement gives the same rows, renumbering a year inline, but Oracle takes longer to plan one of temporal
arithmetic: seconds, and over a minute for a moment two `CALL`s deep. A statement of the query DSL that would call one
the session may not call (a comparison of a list or a map that holds a null with another, or an order by arithmetic of
a property, which may hold a date) is refused with a `DrivineError` that names it, where compiled Cypher is compiled
again without a comparison's function.

On Postgres, `DATABASE_NAME` is the database, and `DATABASE_DEFAULT_GRAPH_PATH` names the graph, `drivine` by default:
see [Postgres](#postgres). On either, Cypher you write is compiled to SQL: see
[Cypher on a relational database](03-cypher-on-a-relational-database.md#cypher-on-a-relational-database).

**Store layout.** Oracle, Postgres and SQLite hold a graph in store layout 2, as drivine4j and Drivine for TypeScript
do: a temporal value is kept as a tagged map, a map key that begins with `$` gains one more, and bytes are kept as the
list of their signed values. A float is held as a Cypher `CREATE` holds it: on Oracle as a binary double, NaN and the
infinities too, on Postgres a whole one with its fraction (`2.0`), and on SQLite an infinity as `9e999`; Postgres's NaN
and infinities, and SQLite's NaN, which their JSON holds as no number, are refused with a `DrivineError` before anything
is written. A `NodeId` that is a float is found by a load, a delete, `edges.relate` and `edges.load_related` as the store
holds it, as a save finds it. The store's stamp records the layout. On a provider's first connection, before anything is
made, the shared module judges the graph: a new one is made in this layout, and one in an earlier layout, or none, that
holds none of your nodes is made this layout's (what the earlier layout left is dropped; nothing is migrated). A graph
in an earlier layout that holds nodes, or one a later Drivine wrote, is refused on that connection and every one after
it with a `StoreLayoutError`, from `drivine` and `drivine.sync`, whose message says what to do (reload it with this
version, or upgrade) and whose `graph` names the graph.

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
`<graph>_node (nid, labels, props, stamp, words)` and `<graph>_edge (eid, src, dst, type, props)`, with `labels` and
`props` as `jsonb` and a GIN index over the labels, so a node carries any number of them; `<graph>_schema`, the indexes
and constraints Drivine made; and `<graph>_write`, the function a save's steps are carried out by, as one statement that
is applied whole or not at all.

- A statement you write yourself is SQL, unless written as Cypher (see
  [Cypher on Postgres, Oracle and SQLite](04-statements.md#cypher-on-postgres-oracle-and-sqlite)). Parameters are named `$name`, as in Cypher, and a
  statement is bound only the ones it names outside quoted text and comments: the `$t` of `'$t'` or `$q$ $t $q$` is
  none. A row comes back as a dict of its columns, a `jsonb` column as Python values.
- A raw `where`, for a load or a delete, is SQL over the root's row `n`: its `props`, `labels` and `stamp`, say
  `(n.props ->> 'name') = $name`, read over the row a statement calls it by, `n_` (the row the shared module's
  fragments read). A property of the root may be written as Cypher writes it, `n.name = $name` (or ``n.`a name` ``),
  and is read as that property, as text, and one whose stored name begins with `$`, ``n.`$score` ``, by the name the
  store holds it by; a property compared with `true` or `false`, or with a parameter bound to `True` or `False`, is
  compared with the text a JSON boolean is read as. To compare one with a number, read it as a number in SQL:
  ``CAST(n.`$score` AS NUMERIC) > $least``. A date in a raw `where` is compared with the property's JSON text, the tag
  a save holds it as, so `(n.props ->> 'at') = $when` finds nothing: compare dates in the query DSL.
- A number is compared as a number only where one is stored: `'7'` is not `7`, as on the graph engines.
- **Behaviour change:** text is compared and ordered by its code points, as Neo4j orders it, in the query DSL's
  conditions (`gt`, `lt` and the rest on text), orders, seeks and grouped reads, as in compiled Cypher
  ([Cypher on Postgres, Oracle and SQLite](04-statements.md#cypher-on-postgres-oracle-and-sqlite)): their SQL is the
  shared module's ([drivine-cypher-spec#235](https://github.com/liberation-data/drivine-cypher-spec/issues/235)), in
  the `C` collation (`COLLATE "C"`), where the DSL used the database's collation before. So `a, B, b` sorts as
  `B, a, b`, as on SQLite and Neo4j, where it sorted `a, b, B`.
- A save checks the stamp, as on the graph engines. A save turned away by a serialization failure (`40001`) or a
  deadlock (`40P01`) is run again.
- Index the id of each class you save: a range index or a uniqueness constraint is a partial index over the label's
  nodes, which finds a node by its id.
- `matches` takes a Postgres regular expression, matched against the whole value.
- A vector index is a pgvector HNSW index over the embeddings of the label's nodes of its size; `load_nearest` is
  approximate through it, and exact with no index made. A full-text index is a trigram index over the text a search
  reads, which narrows the search; `load_matching` scores the same with it and without it, and reads words, a
  `"phrase"`, `+must`, `-not` and a `prefix*`. A word is a run of letters as Unicode's Alphabetic holds them (with the
  vowel signs of Indic scripts), decimal digits and underscores, as the text's `[[:alnum:]_]` reads it: `m²x` is the
  phrase `m x`. In a phrase a `*` is text, as on Neo4j: `"of sou*"` is the phrase `of sou`. It scores by BM25, as
  Lucene and so Neo4j's full-text index do (`k1` 1.2, `b` 0.75): a term weighs as it is rare among the label's nodes,
  and a text's counts against its length, in characters, beside the label's average. A term written again at once,
  `kiwi kiwi kiwi`, is counted each time. A `prefix*` adds the same to every node that holds it, however often and
  however rare, as Lucene's prefix query does: in `kiw* melon`, a node with `kiwi` and no `melon` scores about 0.54 of
  one with both, as on Neo4j. Oracle without Oracle Text scores the same way. A dotted name, as `org.example.models`,
  is words here, split at each dot, as it is on Oracle and SQLite: `example models` finds it, and the name itself is
  the phrase of its words. Neo4j's default analyzer keeps such a name one token, which the whole name finds and
  `example models` doesn't. drivine4j's `FullTextIndexSpec` picks another analyzer there; drivine-python doesn't take
  one yet.
- An index or a uniqueness constraint, a vector index's HNSW index among them, is built with no parallel workers
  (`max_parallel_maintenance_workers` 0), and the session's own setting is put back after, whether the build passed or
  failed. A session that builds an index in parallel keeps a shared memory segment in `/dev/shm` for as long as it
  lives, and the pool keeps it alive: in a container's 64 MB, Docker's default, enough of them leave too little for a
  later HNSW build, which asks for nearly all of `maintenance_work_mem` at once and fails with `could not resize shared
  memory segment ... No space left on device`. A large build takes longer for it. drivine4j closes the session after
  the build instead, and keeps its parallel workers; psycopg_pool would log a warning for each connection given back
  closed.

### SQLite

SQLite keeps a graph in a file, with no server: drivine4j's store, in the same tables as Postgres, so a file one library
wrote, drivine4j, Drivine for TypeScript or this one, the others read and write as it is. `pip install "drivine[sqlite]"`
installs what it takes: [apsw](https://github.com/rogerbinns/apsw), whose wheels bundle a SQLite of their own (3.53.2 or
later). So the store runs on the same SQLite whatever SQLite Python was built
with, as drivine4j's and Drivine for TypeScript's run on the one their drivers bundle. Models, the query DSL and the
object manager are the same as on the other stores; the persistence manager runs plain SQL, with cursors and
transactions, and Cypher you write is compiled to SQLite's SQL, as on Postgres and Oracle: see
[Cypher on Postgres, Oracle and SQLite](04-statements.md#cypher-on-postgres-oracle-and-sqlite). SQLite 3.53.2 writes a
float as text to 17 significant digits (3.53.0 and 3.53.1 still write 15), so a float compiled Cypher gives is the one a graph engine gives, and it runs
a `CALL { … }` that groups by a variable it imports. A SQLite older than 3.53.2, as an apsw built against an older
SQLite of the system runs, is refused as a connection opens.

**Breaking change.** SQLite now goes through apsw and the SQLite it bundles: `pip install "drivine[sqlite]"` brings
it, and `pip install drivine` alone no longer opens a SQLite store. Python's own `sqlite3` module and `pysqlite3` are no
longer used, and Drivine no longer warns that floats may lose their last digits, as no SQLite it runs on does. A
failure inside a function Drivine registers, such as a regular expression that doesn't compile, is raised as itself,
and SQLite's own failures are apsw's (`apsw.Error`, `apsw.ConstraintError`, `apsw.BusyError` …), not `sqlite3.Error`.
A named parameter given no value, or a text of more than one statement, is refused with `DrivineError`, not
`sqlite3.ProgrammingError`. The SQLite file is the same: one written before reads and writes as it did (#376).

```
DATABASE_TYPE=SQLITE
DATABASE_NAME=/var/lib/app/graph.db   # the file's path: there is no host, port, user or password
DATABASE_DEFAULT_GRAPH_PATH=drivine   # names the graph's tables; this is the default
```

Or in code, `registry.builder().database_type(DatabaseType.SQLITE).database_name("graph.db").register("graph")`. The
file is made if it isn't there, and the graph's tables on the first connection, as on Postgres. `:memory:` is refused:
each pooled connection would open a database of its own. Each connection's calls run on a thread of the connection's
own, in both APIs, started with a 64 MB stack, so a regular expression is answered as deep whichever thread asks; in the
async API one waiting for the file's lock holds up neither the event loop nor another connection.

**How a graph is stored.** `<graph>_node (nid INTEGER PRIMARY KEY AUTOINCREMENT, labels TEXT, props TEXT, stamp
TEXT, words TEXT)`, `<graph>_edge (eid INTEGER PRIMARY KEY AUTOINCREMENT, src, dst REFERENCES <graph>_node ON DELETE
CASCADE, type, props TEXT)` and `<graph>_schema`, the indexes and constraints Drivine made, with the shared module's
tables of counts and its lock (`<graph>_count2`, `<graph>_count2_delta`, `<graph>_lock2`). Labels and properties are
JSON text, not SQLite's binary `jsonb`, and labels are written sorted, with nothing escaped that JSON does not require:
a label is found by its text. The stamp is a column. In the query DSL a moment compares and orders as Cypher's does:
by its instant, and equal only to one at the same offset.

The id of a node or relationship that is deleted is never given to another, within a statement, a transaction or after
it, as on Oracle and Postgres: an element id an application holds names the element it was read from or nothing.
SQLite keeps the greatest id each table has given in its own table, `sqlite_sequence`; deleting every row, as a test's
cleanup does, leaves it, so ids go on from there. A file whose graph tables an earlier Drivine made, with `INTEGER
PRIMARY KEY` alone, which gives a new row the id of the last one deleted, is refused on the provider's first
connection and on every one after it, with a `ReusedIdsError`, from `drivine` and `drivine.sync`, that names the
graph and the file. Its `graph` is the graph's name, as the provider was given it, and its `path` the file, `None` for a
database in memory or in a temporary file. There is no migration: make a new file and save the graph into it again.

- A save is the plan of steps Postgres's write function carries out, carried out by a function Drivine registers on
  each connection, `drivine_write`, under a savepoint: applied whole or not at all, in a transaction or not. The other
  functions a statement calls are registered too: `drivine_delete`, `drivine_lower` (Unicode's lower
  case, where SQLite's lowers ASCII), `drivine_similarity` and `drivine_regexp_count`, and the shared module's, loaded
  from its SQLite extension in the `drivine-cypher-compiler` wheel, `REGEXP` among them. A statement of yours may call
  them; a connection you open yourself has none of them.
- A statement Drivine writes that gives back documents opens with `/* drivine:json */`, and its text values are read
  as JSON. One you write is read as it is: a row is a dict of its columns, and `labels` and `props` are text.
- A raw `where` is SQL over the root's row `n`, as on Postgres, read over the row a statement calls it by, `n_` (the
  row the shared module's fragments read); a property written as Cypher writes it, `n.name`, is read as that property,
  as text, and one whose stored name begins with `$`, ``n.`$score` ``, by the name the store holds it by; one compared
  with `true` or `false`, or with a parameter bound to `True` or `False`, is compared with the text a JSON boolean is
  read as, while SQL of your own over `n.props` reads one as SQLite does, `1` or `0`, as the parameter is bound. To
  compare one with a number, read it as a number in SQL: ``CAST(n.`$score` AS NUMERIC) > $least``. A date in a
  raw `where` is compared with the property's JSON, the tag a save holds it as, so `n.at = $when` finds nothing: compare
  dates in the query DSL.
- A range index or a uniqueness constraint is a partial index over the property as a statement reads it, for the
  label's nodes. A vector or a full-text index is recorded and nothing is made for it: `load_nearest` and
  `load_matching` measure every node of the label, and `load_matching` scores by BM25, as Postgres does.
- `matches` takes a regular expression in Java's dialect, as on drivine4j, matched against the whole value: a
  `REGEXP`, the shared module's, reads it as Java does, and refuses what it can't read so, such as a look-behind.

**What a transaction gives.** Each connection writes ahead to a log, enforces foreign keys and waits for a locked file
for up to its busy timeout, 30 seconds by default.

- There is one writer at a time. A transaction takes the file's write lock as it begins (`BEGIN IMMEDIATE`), so even a
  transaction that only reads waits for the lock, for up to the busy timeout. One that holds it longer than 5 seconds
  is logged as a warning when it ends, committed, rolled back or released.
- Reads outside a transaction don't wait for a writer: each reads the file as it last was.
- Saves, patches and deletes outside a transaction are run again when SQLite turns them away as contended
  (`SQLITE_BUSY`).
- Each statement Drivine writes applies whole or not at all.
- A connection you open yourself, with `sqlite3.connect`, has none of this: no foreign keys, no write lock taken as a
  transaction begins, no warning, Python's wait of 5 seconds for a locked file rather than 30, and none of Drivine's
  functions. It does write ahead to the log, as the file keeps that mode once Drivine has set it.

Drivine reads `os.environ`, and doesn't load `.env` files itself: use python-dotenv, pydantic-settings, or your
platform's way.
