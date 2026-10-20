from __future__ import annotations

from pipilgrim import PilgrimAssembly

from drivine.connection.registry import DEFAULT, DatabaseRegistry
from drivine.manager.persistence_manager import PersistenceManager, PersistenceManagerFactory


class DrivineAssembly(PilgrimAssembly):
    """
    Drivine's components, for an application's pi-pilgrim assembly to use:

        class AppAssembly(PilgrimAssembly):
            def __init__(self):
                self.drivine = AssemblyHolder.shared(DrivineAssembly)
                super().__init__()

            def route_repository(self) -> RouteRepository:
                return self.shared(lambda: RouteRepository(self.drivine.persistence_manager()))

    The databases come from the environment by default. To register them in code, subclass it and override
    `register_databases`.
    """

    def __init__(self) -> None:
        self._registry_made = False
        super().__init__()

    def make_bindings(self) -> None:
        self.make_injectable(self.registry, by_type=DatabaseRegistry)
        self.make_injectable(self.persistence_manager, by_type=PersistenceManager)

    def register_databases(self, registry: DatabaseRegistry) -> None:
        """
        Registers the application's databases. By default, the one the environment describes as `DATABASE_TYPE`,
        `DATABASE_HOST` and so on.
        """
        registry.build_or_resolve_from_env()

    def registry(self) -> DatabaseRegistry:
        return self.shared(lambda: self._registered(DatabaseRegistry()))

    def persistence_manager_factory(self) -> PersistenceManagerFactory:
        return self.shared(lambda: PersistenceManagerFactory(self.registry()))

    def persistence_manager(self, database: str = DEFAULT) -> PersistenceManager:
        """
        The persistence manager for a database, which runs each statement in the current transaction if there is one.
        """
        return self.shared(lambda: self.persistence_manager_factory().get(database))

    async def close(self) -> None:
        """
        Closes every database's driver or pool. Call it once, when the application stops.
        """
        if self._registry_made:
            await self.registry().close()

    def _registered(self, registry: DatabaseRegistry) -> DatabaseRegistry:
        self.register_databases(registry)
        self._registry_made = True
        return registry
