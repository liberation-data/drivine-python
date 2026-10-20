from __future__ import annotations

from drivine.connection.database_registry import DEFAULT, DatabaseRegistry
from drivine.drivine_error import DrivineError
from drivine.manager.delegating_persistence_manager import DelegatingPersistenceManager
from drivine.manager.non_transactional_persistence_manager import NonTransactionalPersistenceManager
from drivine.manager.persistence_manager import PersistenceManager
from drivine.manager.persistence_manager_type import PersistenceManagerType
from drivine.manager.transactional_persistence_manager import TransactionalPersistenceManager


class PersistenceManagerFactory:
    """
    Makes the persistence managers for each registered database, once.
    """

    def __init__(self, registry: DatabaseRegistry) -> None:
        self.registry = registry
        self._managers: dict[str, DelegatingPersistenceManager] = {}

    def get(self, database: str = DEFAULT, type: PersistenceManagerType = "DELEGATING") -> PersistenceManager:
        manager = self._managers.get(database)
        if manager is None:
            provider = self.registry.connection_provider(database)
            if provider is None:
                raise DrivineError(f"No database is registered under name: {database}")
            manager = DelegatingPersistenceManager(
                TransactionalPersistenceManager(provider, database),
                NonTransactionalPersistenceManager(provider, database),
            )
            self._managers[database] = manager
        if type == "DELEGATING":
            return manager
        if type == "TRANSACTIONAL":
            return manager.transactional
        if type == "NON_TRANSACTIONAL":
            return manager.non_transactional
        raise DrivineError(f"Invalid PersistenceManagerType: {type}")
