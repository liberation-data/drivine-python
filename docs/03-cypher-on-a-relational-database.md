## Cypher on a relational database

Write a statement in Cypher once and run it on any store Drivine supports. Neo4j, Memgraph and FalkorDB run it as
written. On **Postgres**, **Oracle** and **SQLite**, the SQL stores drivine-python supports, Drivine compiles it to
SQL over the graph's node and edge tables, and the rows come back as a graph engine would give them. The tables are
drivine4j's, so a graph written from Python is read alike from the JVM and Node.

The compiler is the one drivine4j and Drivine for TypeScript run, the shared module of the
[Cypher spec](https://github.com/liberation-data/drivine-cypher-spec), here the native library of the
`drivine-cypher-compiler` wheel, loaded on the first Cypher statement compiled for a SQL store. Each SQL store's extra
installs it (`pip install "drivine[postgres]"`, `[oracle]`, `[sqlite]`), pinned to the compiler of the spec drivine is
tested on, on the platforms it has a wheel for: Linux with glibc (x86-64 and arm64), macOS on Apple Silicon, and Windows
x64. Without it, a Cypher statement for a SQL store is refused with a `DrivineError` that names the extra to install.
Kotlin/Native, which builds it, has no target for Intel Macs or for musl Linux (Alpine), so there is no wheel for them,
and a SQL store's extra doesn't install there. Neo4j, Memgraph and FalkorDB work there as they do anywhere, and a Cypher
statement for a SQL store is refused with a `DrivineError` naming the platform.

Full-text search, `load_matching`, is the module's on every store: the query read, BM25, each SQL store's ranked
candidates and each graph engine's search head. A graph engine's extra doesn't install the module, as it has no wheel
for every platform the graph engines run on, so a search on Neo4j, Memgraph or FalkorDB needs it installed beside the
extra (`pip install drivine-cypher-compiler`), and without it is refused with a `DrivineError` saying so. Every other
statement on a graph engine is sent as it is, and needs no compiler.

Postgres is the quickest to try:

```
docker run -d -p 5432:5432 -e POSTGRES_USER=app -e POSTGRES_PASSWORD=app -e POSTGRES_DB=app pgvector/pgvector:pg16
pip install "drivine[postgres]"
```

```
DATABASE_TYPE=POSTGRES
DATABASE_HOST=localhost
DATABASE_PORT=5432
DATABASE_NAME=app
DATABASE_USER=app
DATABASE_PASSWORD=app
DATABASE_DEFAULT_GRAPH_PATH=drivine   # names the graph's tables; this is the default
```

On Oracle 23.9 or later (`pip install "drivine[oracle]"`), `DATABASE_NAME` is the service:

```
DATABASE_TYPE=ORACLE
DATABASE_HOST=localhost
DATABASE_PORT=1521
DATABASE_NAME=FREEPDB1
DATABASE_USER=app
DATABASE_PASSWORD=…
DATABASE_DEFAULT_GRAPH_PATH=drivine
```

Drivine needs Oracle 23.9 because Cypher's `randomUUID()` is Oracle's `UUID()`, a random version 4 UUID, which Oracle
added in 23.9. A provider reads the version python-oracledb found on its first connection, which needs no grant. On an
older Oracle, plain SQL still runs, and a Cypher statement is refused with the version found.

Then hand the persistence manager a statement written with `cypher_statement` (or `cypher_file`). A plain string is
taken for SQL on these stores, as it is taken for Cypher on a graph engine:

```python
import asyncio

from drivine import DrivineAssembly, QuerySpecification, cypher_statement


async def main() -> None:
    async with DrivineAssembly() as drivine:
        pm = drivine.persistence_manager()
        await pm.execute(
            QuerySpecification(
                cypher_statement(
                    "MERGE (a:Person {uuid: $a}) SET a.name = 'Ada' "
                    "MERGE (b:Person {uuid: $b}) SET b.name = 'Grace' "
                    "MERGE (a)-[:KNOWS {since: 2024}]->(b)"
                )
            ).bind({"a": "p1", "b": "p2"})
        )

        friends = await pm.query(
            QuerySpecification(
                cypher_statement(
                    "MATCH (:Person {name: $name})-[k:KNOWS]->(f:Person) RETURN {name: f.name, since: k.since}"
                )
            ).bind({"name": "Ada"})
        )
        print(friends)  # [{'name': 'Grace', 'since': 2024}]


asyncio.run(main())
```

Leaving the assembly closes the pool. A write is one statement, applied whole or not at all. The graph's tables are made on the first connection when they
aren't there, so the program runs on a new database as it is. The same program runs unchanged on Neo4j, Memgraph and
FalkorDB.

A statement is compiled as written, and nothing is added to it. So a write of a
[`CaseInsensitive`](06-fragments-and-views.md#lookups-that-ignore-case) property leaves the lowercased copy the object
manager keeps beside it as it was, on these stores as on the graph engines: set the copy in the same statement, or
refill it afterwards ([how](06-fragments-and-views.md#cypher-of-your-own)).

A node's id, `id(n)` or `elementId(n)`, is the key of its row, and a relationship's is the key of its own. Where the
row must pass a comparison of one by `=` or `IN`, as a `WHERE` or one of its conditions joined by `AND` must be passed,
the store reads the row by that key: `MATCH (n) WHERE id(n) = $id`, `MATCH (n) WHERE id(n) IN $ids` and
`UNWIND $ids AS i MATCH (n) WHERE id(n) = i` read a row for each id, not every node for each. The comparison is still
Cypher's, so `5.0` is the id `5`, and a text, a list or null is no id. Under `NOT`, beside an `OR`, or as a value
(`RETURN id(n) = $id`), it reads every row it compares, as an expression of a property does. Oracle may still read
the nodes of a label the pattern names (`MATCH (n:Person) WHERE id(n) = $id`) by the index over labels, as it does for
a key written in SQL, `nid = 6`; without the label it reads the key.

On Postgres, a pattern's property compared with a value a row holds (`UNWIND $rows AS row MATCH (n:Person {id:
row.id})`, `UNWIND $ids AS id MATCH (n:Person {id: id})`, or `WHERE n.id = row.id`) is read through the property's
range index or uniqueness constraint, as one compared with a text given is: beside the comparison stands a test of the
property's text among the texts the value can be stored as, a number's with no zeros after its point and with each
count of them up to 16, since `numeric` keeps the scale a product makes (`0.25 * 10` is stored as `2.50`)
([#429](https://github.com/liberation-data/drivine-python/issues/429)). This holds where the rows are known to hold no
list or map at the key: a parameter's, or a list written of literals, maps, lists and parameters, as APOC's export
writes its rows. A row that may hold a list, or a null in a list or a map, is compared as JSON alone, as before, which
reads every node of the label for each row. Oracle compares such a value as JSON alone for now: the compiler can read
it through the labelled index where the request names the graph's indexed properties (`indexedProperties`), and
the connection doesn't name them yet ([drivine4j#638](https://github.com/liberation-data/drivine4j/issues/638)).

A `DELETE` deletes the nodes and relationships of all its rows at once, a statement for each table, not one for each
row: the relationships first, then the nodes, a node's relationships going with it where it is `DETACH`. One that ends
the statement and deletes one variable, a node with `DETACH` or a relationship, of a match that binds only nodes and
relationships, reads the ids from the match itself, so `MATCH (n) DETACH DELETE n` is one SQL `DELETE`. In a `FOREACH`
or a `CALL { … }` that writes, each row is still written alone, as in Cypher.

Any other write, a `CREATE`, `MERGE`, `SET` or `REMOVE`, and a `CALL { … }` that writes and returns, is a loop over its
rows, and keeps each row it leaves in a list of the store's own that grows in place: an array of `jsonb` in PL/pgSQL, a
`JSON_ARRAY_T` in PL/SQL, a list in Drivine's function on SQLite. It is read as one JSON list once the loop is done, so
the loop takes time in proportion to its rows. Appended to a JSON list, each row copied the list, and ten times the rows
took about a hundred times as long ([#419](https://github.com/liberation-data/drivine-python/issues/419)). On Oracle, a
query that reads the row a `FOREACH` or a `CALL { … }` is written for is planned for one row (`CARDINALITY`): planned
for the 8,168 rows Oracle guesses a `JSON_TABLE` of a variable holds, it read every node for each row, by a hash join.

**What compiles.** What each statement gives, on each store, is the
[Cypher spec](https://github.com/liberation-data/drivine-cypher-spec), shared by the three Drivine libraries;
drivine-python passes it at v0.37.0 (pinned in [`tests/cypher/VERSION`](../tests/cypher/VERSION)). Reads, writes,
`UNION`, `CALL { … }`, `EXISTS { … }`, variable-length relationships, `shortestPath` and quantified path patterns all
compile. Cypher the compiler doesn't take yet is refused by name, with a `DrivineError`, before anything is sent to the
database.

Neo4j's schema DDL (`CREATE INDEX`, `CREATE CONSTRAINT`, `DROP INDEX`, `DROP CONSTRAINT`, `CALL db.awaitIndexes`) is no
query: given as Cypher to a `PersistenceManager`, it is applied to the store's schema, as
[Neo4j schema DDL on the SQL stores](07-schema.md#neo4j-schema-ddl-on-the-sql-stores) describes, and the compiler alone
refuses it.

**How complete is it?** The [openCypher TCK](https://github.com/opencypher/openCypher/tree/main/tck) is openCypher's
own test suite: 3,896 scenarios, each a graph, a query, and the rows or the error the query must give.
[drivine4j](https://github.com/liberation-data/drivine4j) runs all of them through its compiler on each store. The
scores below are measured on drivine4j, whose compiler drivine-python runs too. They are drivine4j's `main` on 31 October 2026.

| store | scenarios passed | of all 3,896 | leaving out the 583 refused on purpose |
|---|---:|---:|---:|
| Postgres | 2,905 | 74.6% | 87.7% |
| Oracle | 2,887 | 74.1% | 87.1% |
| Neo4j | 3,821 | 98.1% | (none refused) |

The 583 refused on purpose are Cypher the compiler doesn't take yet, refused by name with a clear error: temporal values
built from a week, a quarter or another date, a truncation that sets nanoseconds or a named time zone, `rand()`, a path
as a value, and a few more.

Most of the rest are not wrong answers. Of the 408 left on Postgres:

- 172 are in openCypher's temporal features, which are being compiled now: 130 give another result, and 42 write a date
  or a time in a form Drivine doesn't read yet, such as `2015-W30-2`, `20150721` or a named zone, and fail with an error
  of Drivine's own.
- 116 are about errors: 77 statements that run where the TCK expects an error, and 39 that Drivine fails where the TCK
  expects rows.
- 59 are steps the harness can't run through Drivine, such as a test procedure or a CSV file.
- 53 give another result outside the temporal features, and 8 make the database fail.

Oracle's are split alike. The comparison is lenient where Drivine's results are shaped differently from the TCK's: a
node is compared by its properties, and any error meets an expected one. Counting an expected error only where Drivine
raised it, before the database, Postgres passes 2,865 and Oracle 2,852.

Neo4j runs the same scenarios through the same harness and misses only the 59 it can't run and 16 where Neo4j itself
differs from the TCK, so the harness is sound and the gap on the SQL stores is the compiler's. Every night drivine4j
holds each store to the scenarios it passed, one by one, so none can slip back unnoticed.
[drivine4j's TCK.md](https://github.com/liberation-data/drivine4j/blob/main/TCK.md) has the method and how to run it.

**What differs.** Each is written up where its store is:

- Parameters are named, `$name`, and not a list; values are compared as JSON, so `'7'` is not `7`; dates and times
  come back as ISO 8601 text; on Oracle a cursor over a write is refused: see
  [Cypher on Postgres, Oracle and SQLite](04-statements.md#cypher-on-postgres-oracle-and-sqlite).
- On Postgres, float arithmetic, `sum()` and `avg()` are exact decimal, so they are more accurate: `0.1 + 0.2` is
  `0.3` there, where doubles give `0.30000000000000004`. A result differs from Neo4j's only around its 16th significant
  digit, and still comes back as a float. A list or a map read from a property orders the texts in it by the
  database's collation, where other text is ordered by its code points, as Cypher does
  ([Cypher on Postgres, Oracle and SQLite](04-statements.md#cypher-on-postgres-oracle-and-sqlite)).
- On Oracle, an empty text is null, and a text past 4,000 bytes is measured, cut, changed, searched and reversed
  whole; a quantified path pattern's `WHERE`, `reduce` and `map[key]` take less than on Postgres
  ([Cypher on Postgres, Oracle and SQLite](04-statements.md#cypher-on-postgres-oracle-and-sqlite)).

The object manager needs no Cypher at all, on any store: [the quick start](01-quick-start.md#quick-start) runs as written on Postgres and
Oracle.

On Postgres and Oracle it is also the faster way. It runs SQL of its own, written for the store's tables, where a Cypher
statement is compiled to SQL first; and on Oracle its statements stay prepared, where a compiled one is prepared again
each time it runs (see [Connecting](02-connecting.md#connecting)).
