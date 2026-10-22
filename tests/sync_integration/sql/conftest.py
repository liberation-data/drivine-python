# Written by scripts/unasync.py from tests/integration/sql/conftest.py:
# change that, and run the script again.
import pytest

from drivine.sync import PersistenceManager, PersistenceManagerFactory, StatelessGraphObjectManager
from tests.sync_integration.conftest import ORACLE, POSTGRES

SQL_STORES = [*(["postgres"] if POSTGRES else []), *(["oracle"] if ORACLE else [])]
"""
The SQL stores an object manager keeps a graph in, as configured: Postgres as the tests' own environment configures it,
and Oracle with ORACLE_TESTS=true. These suites run on each.
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
