# Written by scripts/unasync.py from tests/integration/conftest.py:
# change that, and run the script again.
import os
from collections.abc import Iterator

import pytest

from drivine.connection.sqlite.sqlite_session import FULL_FLOATS_VERSION, full_floats, sqlite_module
from drivine.sync import (
    DatabaseRegistry,
    DatabaseType,
    PersistenceManager,
    PersistenceManagerFactory,
    QuerySpecification,
)
from tests.sync_integration.sql_cypher_fixtures import for_store

ORACLE = os.environ.get("ORACLE_DATABASE_TYPE") == "ORACLE"
"""Whether Oracle is configured: with ORACLE_TESTS=true."""

POSTGRES = os.environ.get("POSTGRES_DATABASE_TYPE") == "POSTGRES"
"""Whether Postgres is configured, as the tests' own environment configures it."""

SQLITE = os.environ.get("SQLITE_DATABASE_TYPE") == "SQLITE"
"""Whether SQLite is configured, as the tests' own environment configures it: a file of the run's own."""

ENGINES = [
    "default",
    "memgraph",
    "falkordb",
    *(["postgres"] if POSTGRES else []),
    *(["oracle"] if ORACLE else []),
    *(["sqlite"] if SQLITE else []),
]
"""
The registered names of the databases each test runs on: Neo4j, Memgraph and FalkorDB, Postgres and SQLite as the
tests' own environment configures them, and Oracle when set.
"""

SQL_STORES = ("postgres", "oracle", "sqlite")
"""The stores that keep a graph in SQL tables."""

SQLITE_ROUNDS_FLOATS = not full_floats(sqlite_module())
"""Whether the SQLite the tests run on writes a float to 15 significant digits, as before 3.53 (#376)."""

SQLITE_BEFORE_3_53 = sqlite_module().sqlite_version_info < FULL_FLOATS_VERSION
"""
Whether the SQLite the tests run on is older than 3.53, which refuses a subquery in json_each that groups by a column of
its row (#376).
"""


def _reads_floats_exactly() -> bool:
    connection = sqlite_module().connect(":memory:")
    try:
        texts = ["1.5e300", "-1.5e300", "1.7976931348623157e308", "1.0e-310", "5e-324"]
        return all(connection.execute("SELECT json(?) ->> '$'", (it,)).fetchone()[0] == float(it) for it in texts)
    finally:
        connection.close()


SQLITE_READS_FLOATS_INEXACTLY = not _reads_floats_exactly()
"""
Whether the SQLite the tests run on reads a float from text less exactly, 1.5E300 as 1.4999999999999998E300: one before
3.53 does where it has no long double wider than a double, as on arm64, and not on x86-64 (#376).
"""


@pytest.fixture(scope="session")
def registry() -> Iterator[DatabaseRegistry]:
    registry = DatabaseRegistry()
    registry.build_or_resolve_from_env()
    registry.build_or_resolve_from_env("memgraph")
    registry.build_or_resolve_from_env("falkordb")
    if POSTGRES:
        registry.build_or_resolve_from_env("postgres")
    if ORACLE:
        registry.build_or_resolve_from_env("oracle")
    if SQLITE:
        registry.build_or_resolve_from_env("sqlite")
    yield registry
    registry.close()


@pytest.fixture(scope="session")
def factory(registry: DatabaseRegistry) -> PersistenceManagerFactory:
    return PersistenceManagerFactory(registry)


def end_oracle_sessions(registry: DatabaseRegistry) -> None:
    """
    Ends the pool of each Oracle provider the registry holds, the tests' own among them, so that its sessions end and
    give back the PGA each kept: an Oracle session keeps what it grew, and Oracle Free holds 2 GB for all of them
    (#402). A provider's next connection opens a new pool, and makes the graph's tables again where they aren't there.
    """
    for provider in registry.providers:
        if provider.type is DatabaseType.ORACLE:
            provider.end()


@pytest.fixture(scope="module", autouse=ORACLE)
def oracle_sessions_ended(registry: DatabaseRegistry) -> Iterator[None]:
    """
    The Oracle sessions a module's tests opened, ended after it: in the registry's pools, which grow to their size when
    tests run managers at once, and in the providers tests register (#402).
    """
    yield
    end_oracle_sessions(registry)


@pytest.fixture(params=ENGINES)
def pm(request: pytest.FixtureRequest, factory: PersistenceManagerFactory) -> Iterator[PersistenceManager]:
    manager = factory.get(request.param)
    if manager.type in (DatabaseType.ORACLE, DatabaseType.POSTGRES, DatabaseType.SQLITE):
        # A node's relationships go with it: the relationship table cascades.
        manager.execute(QuerySpecification("DELETE FROM drivine_node"))
    else:
        manager.execute(QuerySpecification("MATCH (n) DETACH DELETE n"))
    # A test written in Cypher sends its statements as they are: on a SQL store they are compiled, not taken for SQL.
    yield for_store(manager) if request.node.get_closest_marker("written_in_cypher") else manager


CYPHER_STORES = [
    "default",
    *(["postgres"] if POSTGRES else []),
    *(["oracle"] if ORACLE else []),
    *(["sqlite"] if SQLITE else []),
]
"""
The registered names of the stores a Cypher statement is checked on, to give the same rows: Neo4j, and the SQL stores it
is compiled for, as the tests' environment has them.
"""


@pytest.fixture(params=CYPHER_STORES)
def cypher_pm(request: pytest.FixtureRequest, factory: PersistenceManagerFactory) -> PersistenceManager:
    """A manager of each store a Cypher statement is checked on: its graph is the test's to clear."""
    return factory.get(request.param)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """
    A test marked `rolls_back` needs multi-statement transactions, which FalkorDB doesn't have. One marked
    `graph_engine_only` runs what a SQL store doesn't take, which the marker says. One marked `fails_on_sql_stores`
    fails on them for the issue it names, until that is fixed. One marked `needs_full_floats`, `needs_exact_float_reads`
    or `needs_sqlite_3_53` fails on SQLite where the tests run on one that rounds floats to 15 digits, reads them from
    text less exactly, or is older than 3.53 (#376).
    """
    for item in items:
        callspec = getattr(item, "callspec", None)
        # The store a test runs on: its `pm`, or in the suites over SQL stores alone, its `store`.
        engine = (callspec.params.get("pm") or callspec.params.get("store")) if callspec is not None else None
        if item.get_closest_marker("rolls_back") and engine == "falkordb":
            item.add_marker(pytest.mark.skip(reason="FalkorDB commits each statement as it runs: nothing rolls back."))
        graph_engine_only = item.get_closest_marker("graph_engine_only")
        if graph_engine_only and engine in SQL_STORES:
            item.add_marker(pytest.mark.skip(reason=f"Graph engines only: {graph_engine_only.args[0]}"))
        fails = item.get_closest_marker("fails_on_sql_stores")
        if fails and engine in SQL_STORES:
            item.add_marker(pytest.mark.xfail(reason=fails.args[0], strict=True))
        # A test over `cypher_pm` runs on the store it names, which the markers above leave as they are.
        store = callspec.params.get("cypher_pm", engine) if callspec is not None else None
        if item.get_closest_marker("needs_full_floats") and store == "sqlite" and SQLITE_ROUNDS_FLOATS:
            reason = "This SQLite writes a float to 15 significant digits (#376)."
            item.add_marker(pytest.mark.xfail(reason=reason, strict=True))
        if item.get_closest_marker("needs_exact_float_reads") and store == "sqlite" and SQLITE_READS_FLOATS_INEXACTLY:
            reason = "This SQLite reads 1.5E300 from text as 1.4999999999999998E300 (#376)."
            item.add_marker(pytest.mark.xfail(reason=reason, strict=True))
        if item.get_closest_marker("needs_sqlite_3_53") and store == "sqlite" and SQLITE_BEFORE_3_53:
            reason = "SQLite before 3.53 groups by no outer column in json_each (#376)."
            item.add_marker(pytest.mark.xfail(reason=reason, strict=True))
