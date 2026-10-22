# Written by scripts/unasync.py from tests/integration/postgres/conftest.py:
# change that, and run the script again.
import os

import pytest

from drivine.sync import PersistenceManager, PersistenceManagerFactory, StatelessGraphObjectManager

ON_POSTGRES = pytest.mark.skipif(
    os.environ.get("POSTGRES_DATABASE_TYPE") != "POSTGRES", reason="Postgres is not configured."
)
"""Skips a suite unless Postgres is configured, as the tests' own environment configures it."""


@pytest.fixture
def pm(factory: PersistenceManagerFactory) -> PersistenceManager:
    """Postgres's persistence manager: these suites run on Postgres alone."""
    return factory.get("postgres")


@pytest.fixture
def gom(pm: PersistenceManager) -> StatelessGraphObjectManager:
    return StatelessGraphObjectManager(pm)
