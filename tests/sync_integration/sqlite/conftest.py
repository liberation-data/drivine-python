# Written by scripts/unasync.py from tests/integration/sqlite/conftest.py:
# change that, and run the script again.
"""
A SQLite file of each test's own: SQLite needs no container, so these suites run wherever the tests do. drivine4j's
SqliteTestDatabase does the same.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from drivine.sync import (
    DatabaseRegistry,
    DatabaseType,
    PersistenceManager,
    PersistenceManagerFactory,
    StatelessGraphObjectManager,
)


@pytest.fixture
def path(tmp_path: Path) -> str:
    """The file the test's store is kept in, not yet made."""
    return str(tmp_path / "graph.db")


@pytest.fixture
def sqlite_registry(path: str) -> Iterator[DatabaseRegistry]:
    registry = DatabaseRegistry()
    registry.builder().database_type(DatabaseType.SQLITE).database_name(path).register("sqlite")
    yield registry
    registry.close()


@pytest.fixture
def pm(sqlite_registry: DatabaseRegistry) -> PersistenceManager:
    return PersistenceManagerFactory(sqlite_registry).get("sqlite")


@pytest.fixture
def gom(pm: PersistenceManager) -> StatelessGraphObjectManager:
    return StatelessGraphObjectManager(pm)
