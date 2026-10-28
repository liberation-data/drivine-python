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
are `neo4j:latest`, `memgraph/memgraph:latest` and `falkordb/falkordb:latest`, as drivine4j's are, so a new engine
release reaches your tests as soon as it is out; pin one with its `<TYPE>_TEST_IMAGE` to hold it back.

Drivine's own tests run on Oracle only with `ORACLE_TESTS=true`: its image is large, and the database wants two
gigabytes. A run without it ends with the line `Oracle tests skipped: set ORACLE_TESTS=true to run them`. CI runs the
Oracle tests in a job of their own, with `pytest -k oracle`. The tests run on the plugin's own images, the latest Neo4j,
Memgraph and FalkorDB, so that an engine release that changes what Drivine reads or writes shows up at once. CI holds
FalkorDB at a release that passes, with `FALKORDB_TEST_IMAGE` in `.github/workflows/ci.yml`: Docker Hub's
`falkordb/falkordb:latest` is sometimes a 4.x patch release
([FalkorDB/FalkorDB#3551](https://github.com/FalkorDB/FalkorDB/issues/3551)). CI's nightly run leaves the pin out and
runs `latest`, so it shows when the pin can move or go.
