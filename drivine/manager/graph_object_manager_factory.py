from __future__ import annotations

from drivine.connection.database_registry import DEFAULT
from drivine.manager.persistence_manager_factory import PersistenceManagerFactory
from drivine.manager.stateless_graph_object_manager import StatelessGraphObjectManager
from drivine.model.model_registry import ModelRegistry


class GraphObjectManagerFactory:
    """
    Makes the object manager for each registered graph database, once. They share one ModelRegistry.
    """

    def __init__(self, persistence_managers: PersistenceManagerFactory, registry: ModelRegistry | None = None) -> None:
        self.persistence_managers = persistence_managers
        self.registry = registry or ModelRegistry()
        self._managers: dict[str, StatelessGraphObjectManager] = {}

    def get(self, database: str = DEFAULT) -> StatelessGraphObjectManager:
        manager = self._managers.get(database)
        if manager is None:
            manager = StatelessGraphObjectManager(self.persistence_managers.get(database), self.registry)
            self._managers[database] = manager
        return manager
