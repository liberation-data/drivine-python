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
