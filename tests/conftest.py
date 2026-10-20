import os

# The datasources the tests run on. Drivine's plugin starts a container for each and points these at it. The host is
# a name that resolves nowhere, so that with the plugin off the tests fail rather than clear a database of yours. To
# run against your own, set the datasource and USE_LOCAL_NEO4J=true, USE_LOCAL_MEMGRAPH=true or USE_LOCAL_FALKORDB=true.
# Oracle runs only with ORACLE_TESTS=true: its image is large and the database wants two gigabytes.
os.environ.setdefault("DATABASE_TYPE", "NEO4J")
os.environ.setdefault("DATABASE_HOST", "test-container.invalid")
os.environ.setdefault("DATABASE_USER", "neo4j")
os.environ.setdefault("MEMGRAPH_DATABASE_TYPE", "MEMGRAPH")
os.environ.setdefault("MEMGRAPH_DATABASE_HOST", "test-container.invalid")
os.environ.setdefault("FALKORDB_DATABASE_TYPE", "FALKORDB")
os.environ.setdefault("FALKORDB_DATABASE_HOST", "test-container.invalid")
if os.environ.get("ORACLE_TESTS") == "true":
    os.environ.setdefault("ORACLE_DATABASE_TYPE", "ORACLE")
    os.environ.setdefault("ORACLE_DATABASE_HOST", "test-container.invalid")
    os.environ.setdefault("ORACLE_DATABASE_NAME", "FREEPDB1")
