
[![PyPI](https://img.shields.io/pypi/v/drivine.svg)](https://pypi.org/project/drivine/)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)

**Docs: [drivine.org](https://drivine.org/py/)**, for Python, TypeScript, and Kotlin & Java.

A graph database client for Python, supporting **Neo4j**, **Memgraph**, **FalkorDB**, **Oracle** (SQL/PGQ) and
**Postgres** as graph stores, plus **Amazon Neptune** through the low-level API. Async first, with a sync API written from it, typed, and fast: one statement per round trip, no session
cache, no lazy loading. It gives you two ways to work with a graph:

1. **PersistenceManager**: a low-level API where you write the queries yourself.
2. **Object manager** (`StatelessGraphObjectManager`): a high-level API that maps Pydantic models, with a query DSL
   whose typed references are stubs generated from your models.

Neptune connects for the `PersistenceManager`; the object manager runs on the others. Cypher you write runs on
Postgres and Oracle too, compiled to SQL: see [Cypher on a relational database](03-cypher-on-a-relational-database.md#cypher-on-a-relational-database).
0.3 is an alpha release: the API may still change. See [the design](../docs/design.md) for how it came about.

> **On Node.js or the JVM?** [Drivine](https://github.com/liberation-data/drivine)
> (`npm install @liberation-data/drivine`) and [drivine4j](https://github.com/liberation-data/drivine4j)
> (`org.drivine:drivine4j`) are its TypeScript and Kotlin & Java twins, with the same object manager, query DSL, schema
> management and engines. The three share a storage format: a store written from any of them is read, stamped and
> checked alike by the others.

```
pip install "drivine[neo4j]"      # Neo4j, Memgraph, Neptune
pip install "drivine[falkordb]"   # FalkorDB
pip install "drivine[oracle]"     # Oracle 23.9 or later, through python-oracledb in thin mode
pip install "drivine[postgres]"   # Postgres 14 or later, through psycopg 3
```

Python 3.11 or newer. [pi-pilgrim](https://github.com/liberation-data/pi-pilgrim), which `DrivineAssembly` is
written with, comes with each; `drivine[pilgrim]` still installs, as it did when pi-pilgrim was an extra.
