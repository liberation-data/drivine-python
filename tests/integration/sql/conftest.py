import pytest

from drivine import PersistenceManager, PersistenceManagerFactory, StatelessGraphObjectManager
from tests.integration.conftest import ORACLE, POSTGRES, SQLITE

SQL_STORES = [*(["postgres"] if POSTGRES else []), *(["oracle"] if ORACLE else []), *(["sqlite"] if SQLITE else [])]
"""
The SQL stores an object manager keeps a graph in, as configured: Postgres and SQLite as the tests' own environment
configures them, and Oracle with ORACLE_TESTS=true. These suites run on each.
"""


@pytest.fixture(scope="module", params=SQL_STORES)
def store(request: pytest.FixtureRequest) -> str:
    """The registered name of the SQL store the suite runs on."""
    return request.param


@pytest.fixture
def pm(factory: PersistenceManagerFactory, store: str) -> PersistenceManager:
    return factory.get(store)


@pytest.fixture
def gom(pm: PersistenceManager) -> StatelessGraphObjectManager:
    return StatelessGraphObjectManager(pm)
