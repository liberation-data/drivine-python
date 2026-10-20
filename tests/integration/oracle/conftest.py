import pytest

from drivine import PersistenceManager, PersistenceManagerFactory, StatelessGraphObjectManager


@pytest.fixture
def pm(factory: PersistenceManagerFactory) -> PersistenceManager:
    """Oracle's persistence manager: these suites run on Oracle alone."""
    return factory.get("oracle")


@pytest.fixture
def gom(pm: PersistenceManager) -> StatelessGraphObjectManager:
    return StatelessGraphObjectManager(pm)
