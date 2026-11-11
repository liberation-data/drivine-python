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

`uv run python scripts/compile_bench.py` times `import drivine`, each SQL store's first compile and its warm compiles
over the Cypher spec in `tests/cypher`. CI's `compile-bench` job runs it on Python 3.13, puts the table in the run's
summary, and fails where the least of at least 7 fresh `import drivine`s is worse by more than 10% than main's, timed
in turn on the same runner (`--import-against`, a checkout of main beside the tree: a runner's CPU type moved import
by a quarter where the code didn't, #496), or where a store's first compile's median, or its uncached compile's median
or p99, is worse than the baseline by more than 10% (`--against tests/bench/compile-baseline.json`): the baseline is
the compiler Drivine had before the shared one, measured by CI before the switch (#467). The baseline's import is
printed for reference only.

The integration tests run on Neo4j, Memgraph, FalkorDB, Postgres, Oracle and SQLite. A test marked `written_in_cypher`
seeds and reads back in Cypher with statements of no language, and on the SQL stores those are sent as Cypher, which is
compiled to SQL (`tests/integration/sql_cypher_fixtures.py`, as drivine4j's `SqlCypherFixtures`). A test marked
`graph_engine_only` runs only on the graph engines, for the reason it gives, and one marked `fails_on_sql_stores` is
expected to fail on the SQL stores until the issue it names is fixed.
