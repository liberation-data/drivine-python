from __future__ import annotations

from drivine.connection.database_registry import DEFAULT
from drivine.dsl.index_advice_policy import IndexAdvicePolicy
from drivine.manager.persistence_manager_factory import PersistenceManagerFactory
from drivine.manager.stateless_graph_object_manager import StatelessGraphObjectManager
from drivine.model.model_registry import ModelRegistry


class GraphObjectManagerFactory:
    """
    Makes the object manager for each registered graph database, once. They share one ModelRegistry.
    """

    def __init__(
        self,
        persistence_managers: PersistenceManagerFactory,
        registry: ModelRegistry | None = None,
        index_advice: IndexAdvicePolicy = IndexAdvicePolicy.WARN,
    ) -> None:
        """`index_advice` is what the managers do with an ordered query no range index serves: FAIL in CI, say."""
        self.persistence_managers = persistence_managers
        self.registry = registry or ModelRegistry()
        self.index_advice = index_advice
        self._managers: dict[str, StatelessGraphObjectManager] = {}

    def get(self, database: str = DEFAULT) -> StatelessGraphObjectManager:
        manager = self._managers.get(database)
        if manager is None:
            manager = StatelessGraphObjectManager(self.persistence_managers.get(database), self.registry)
            manager.index_advice = self.index_advice
            self._managers[database] = manager
        return manager
