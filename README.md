<a href="https://drivine.org/"><img src="https://raw.githubusercontent.com/liberation-data/drivine/master/website/public/images/splash.jpg" alt="Drivine: graph database client for TypeScript, Python and the JVM"></a>

# drivine-python

[![PyPI](https://img.shields.io/pypi/v/drivine.svg)](https://pypi.org/project/drivine/)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)

**Docs: [drivine.org](https://drivine.org/py/)**, for Python, TypeScript, and Kotlin & Java.

<p align="center">
  <a href="https://www.youtube.com/watch?v=uETP0EN_04E" target="_blank">
    <img src="https://raw.githubusercontent.com/liberation-data/drivine/master/website/src/assets/video-poster.jpg" alt="Watch: Drivine, graph RAG on any database (4:26)" width="700">
  </a>
  <br>
  <a href="https://www.youtube.com/watch?v=uETP0EN_04E" target="_blank">▶ Watch Drivine in four and a half minutes: graph RAG on any database</a> (4:26, with captions)
</p>

A graph database client for Python, supporting **Neo4j**, **Memgraph**, **FalkorDB**, **Oracle** (SQL/PGQ) and
**Postgres** as graph stores, plus **Amazon Neptune** through the low-level API. Async first, with a sync API written
from it, typed, and fast: one statement per round trip, no session cache, no lazy loading. It gives you two ways to work
with a graph:

1. **PersistenceManager**: a low-level API where you write the queries yourself.
2. **Object manager** (`StatelessGraphObjectManager`): a high-level API that maps Pydantic models, with a query DSL
   whose typed references are stubs generated from your models.

Cypher you write runs on Postgres and Oracle too, compiled to SQL. 0.3 is an alpha release: the API may still change.

## Install

```
pip install "drivine[neo4j]"      # Neo4j, Memgraph, Neptune
pip install "drivine[falkordb]"   # FalkorDB
pip install "drivine[oracle]"     # Oracle 23ai and 26ai, through python-oracledb in thin mode
pip install "drivine[postgres]"   # Postgres 14 or later, through psycopg 3
```

Python 3.11 or newer.

## Quick start

Describe the database in the environment (`DATABASE_TYPE=NEO4J`, `DATABASE_HOST=localhost`, `DATABASE_USER`,
`DATABASE_PASSWORD`), declare a fragment for a node, then save and load it, each in one statement:

```python
import asyncio
from typing import Annotated

from drivine import DrivineAssembly, NodeFragment, NodeId, refs


class Person(NodeFragment, labels=["Person"]):
    uuid: Annotated[str, NodeId()]
    name: str


async def main() -> None:
    async with DrivineAssembly() as drivine:
        gom = drivine.graph_object_manager()
        await gom.save(Person(uuid="p1", name="Ada"))
        print(await gom.load_all(Person, where=refs(Person).name.eq("Ada")))


asyncio.run(main())
```

Views that compose fragments and relationships, queries, transactions, schema and the sync API are in the
[full quick start](https://drivine.org/py/quick-start/) and the pages after it.

## Documentation

- [drivine.org/py](https://drivine.org/py/): the full documentation.
- [docs/](https://github.com/liberation-data/drivine-python/tree/main/docs) on GitHub: the same pages, as Markdown.
- [RELEASING.md](https://github.com/liberation-data/drivine-python/blob/main/RELEASING.md): how a release is made.
- [Building](https://github.com/liberation-data/drivine-python/blob/main/docs/13-building.md) and
  [AGENTS.md](https://github.com/liberation-data/drivine-python/blob/main/AGENTS.md): building and testing, and notes
  for contributors.
- [Drivine](https://github.com/liberation-data/drivine) and [drivine4j](https://github.com/liberation-data/drivine4j)
  are its TypeScript and Kotlin & Java twins, sharing its storage format.

## License

Apache 2.0.
