## Cypher on a relational database

Write a statement in Cypher once and run it on any store Drivine supports. Neo4j, Memgraph and FalkorDB run it as
written. On **Postgres** and **Oracle**, the two SQL stores drivine-python supports, Drivine compiles it to SQL over
the graph's node and edge tables, and the rows come back as a graph engine would give them. The tables are drivine4j's,
so a graph written from Python is read alike from the JVM and Node.

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

On Oracle 23ai or 26ai (`pip install "drivine[oracle]"`), `DATABASE_NAME` is the service:

```
DATABASE_TYPE=ORACLE
DATABASE_HOST=localhost
DATABASE_PORT=1521
DATABASE_NAME=FREEPDB1
DATABASE_USER=app
DATABASE_PASSWORD=…
DATABASE_DEFAULT_GRAPH_PATH=drivine
```

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

**What compiles.** What each statement gives, on each store, is the
[Cypher spec](https://github.com/liberation-data/drivine-cypher-spec), shared by the three Drivine libraries;
drivine-python passes it at v0.6.5 (pinned in [`tests/cypher/VERSION`](../tests/cypher/VERSION)). Reads, writes,
`UNION`, `CALL { … }`, `EXISTS { … }`, variable-length relationships, `shortestPath` and quantified path patterns all
compile. Cypher the compiler doesn't take yet is refused by name, with a `DrivineError`, before anything is sent to the
database.

**What differs.** Each is written up where its store is:

- Parameters are named, `$name`, and not a list; values are compared as JSON, so `'7'` is not `7`; dates and times
  come back as ISO 8601 text; on Oracle a cursor over a write is refused: see
  [Cypher on Postgres and Oracle](04-statements.md#cypher-on-postgres-and-oracle).
- On Postgres, float arithmetic, `sum()` and `avg()` are exact decimal, so they are more accurate: `0.1 + 0.2` is
  `0.3` there, where doubles give `0.30000000000000004`. A result differs from Neo4j's only around its 16th significant
  digit, and still comes back as a float. A list or a map read from a property orders the texts in it by the
  database's collation, where other text is ordered by its code points, as Cypher does
  ([Cypher on Postgres and Oracle](04-statements.md#cypher-on-postgres-and-oracle)).
- On Oracle, an empty text is null, and a text past 4,000 bytes is measured, cut, changed, searched and reversed
  whole; a quantified path pattern's `WHERE`, `reduce` and `map[key]` take less than on Postgres
  ([Cypher on Postgres and Oracle](04-statements.md#cypher-on-postgres-and-oracle)).

The object manager needs no Cypher at all, on any store: [the quick start](01-quick-start.md#quick-start) runs as written on Postgres and
Oracle.

On Postgres and Oracle it is also the faster way. It runs SQL of its own, written for the store's tables, where a Cypher
statement is compiled to SQL first; and on Oracle its statements stay prepared, where a compiled one is prepared again
each time it runs (see [Connecting](02-connecting.md#connecting)).
