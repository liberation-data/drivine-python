import os

import pytest

# The tests run on the plugin's defaults, the latest Neo4j, Memgraph and FalkorDB, which a user's tests get too, so that
# an engine release that changes what Drivine reads or writes shows up here at once. The Cypher spec (tests/cypher) is
# written for a current Neo4j, as drivine4j runs it. NEO4J_TEST_IMAGE, MEMGRAPH_TEST_IMAGE or FALKORDB_TEST_IMAGE runs
# another.

# The datasources the tests run on. Drivine's plugin starts a container for each and points these at it. The host is
# a name that resolves nowhere, so that with the plugin off the tests fail rather than clear a database of yours. To
# run against your own, set the datasource and USE_LOCAL_NEO4J=true, USE_LOCAL_MEMGRAPH=true, USE_LOCAL_FALKORDB=true or
# USE_LOCAL_POSTGRES=true.
# Oracle runs only with ORACLE_TESTS=true: its image is large and the database wants two gigabytes. CI runs it in a job
# of its own.
os.environ.setdefault("DATABASE_TYPE", "NEO4J")
os.environ.setdefault("DATABASE_HOST", "test-container.invalid")
os.environ.setdefault("DATABASE_USER", "neo4j")
os.environ.setdefault("MEMGRAPH_DATABASE_TYPE", "MEMGRAPH")
os.environ.setdefault("MEMGRAPH_DATABASE_HOST", "test-container.invalid")
os.environ.setdefault("FALKORDB_DATABASE_TYPE", "FALKORDB")
os.environ.setdefault("FALKORDB_DATABASE_HOST", "test-container.invalid")
os.environ.setdefault("POSTGRES_DATABASE_TYPE", "POSTGRES")
os.environ.setdefault("POSTGRES_DATABASE_HOST", "test-container.invalid")
os.environ.setdefault("POSTGRES_DATABASE_NAME", "drivine")
if os.environ.get("ORACLE_TESTS") == "true":
    os.environ.setdefault("ORACLE_DATABASE_TYPE", "ORACLE")
    os.environ.setdefault("ORACLE_DATABASE_HOST", "test-container.invalid")
    os.environ.setdefault("ORACLE_DATABASE_NAME", "FREEPDB1")


ORACLE_SKIPPED = "Oracle tests skipped: set ORACLE_TESTS=true to run them"
"""The line a run without Oracle ends with."""


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    """Says once, at the end of a run without Oracle, that Oracle was left out, so it isn't taken for a full run."""
    if os.environ.get("ORACLE_TESTS") != "true":
        terminalreporter.write_sep("=", ORACLE_SKIPPED, yellow=True, bold=True)
