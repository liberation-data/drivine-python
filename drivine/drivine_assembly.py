from __future__ import annotations

from typing import NoReturn, Self

from pipilgrim import AsyncPilgrimAssembly, AsyncTeardownRequired

from drivine.connection.database_registry import DEFAULT, DatabaseRegistry
from drivine.dsl.index_advice_policy import IndexAdvicePolicy
from drivine.manager.graph_object_manager_factory import GraphObjectManagerFactory
from drivine.manager.persistence_manager import PersistenceManager
from drivine.manager.persistence_manager_factory import PersistenceManagerFactory
from drivine.manager.stateless_graph_object_manager import StatelessGraphObjectManager
from drivine.model.model_registry import ModelRegistry
from drivine.schema.schema_manager import SchemaManager
from drivine.schema.schema_options import SchemaOptions


class DrivineAssembly(AsyncPilgrimAssembly):
    """
    Drivine's components: the registry and the managers. On its own:

        async with DrivineAssembly() as drivine:      # starts Drivine; closes its drivers and pools on the way out
            pm = drivine.persistence_manager()

    Or for an application's pi-pilgrim assembly to use, which closes Drivine when it closes:

        class AppAssembly(PilgrimAssembly):
            def __init__(self):
                self.drivine = AssemblyHolder.shared(DrivineAssembly)
                super().__init__()

            def route_repository(self) -> RouteRepository:
                return self.shared(lambda: RouteRepository(self.drivine.persistence_manager()))

    The databases come from the environment by default. To register them in code, subclass it and override
    `register_databases`.

    When the assembly closes, at the end of `async with DrivineAssembly() as drivine:` or with the assembly that holds
    it, it closes every database's driver or pool, as Spring closes drivine4j's when its context closes: the registry is
    a `shared` component, whose `close` is its teardown. A closed assembly makes no more components.
    """

    def make_bindings(self) -> None:
        self.make_injectable(self.registry, by_type=DatabaseRegistry)
        self.make_injectable(self.persistence_manager, by_type=PersistenceManager)
        self.make_injectable(self.graph_object_manager, by_type=StatelessGraphObjectManager)
        self.make_injectable(self.model_registry, by_type=ModelRegistry)

    def register_databases(self, registry: DatabaseRegistry) -> None:
        """
        Registers the application's databases. By default, the one the environment describes as `DATABASE_TYPE`,
        `DATABASE_HOST` and so on.
        """
        registry.build_or_resolve_from_env()

    def registry(self) -> DatabaseRegistry:
        """The application's databases, registered by `register_databases`. Closed when the assembly closes."""
        return self.shared(DatabaseRegistry, init=self.register_databases)

    def persistence_manager_factory(self) -> PersistenceManagerFactory:
        return self.shared(lambda: PersistenceManagerFactory(self.registry()))

    def persistence_manager(self, database: str = DEFAULT) -> PersistenceManager:
        """
        The persistence manager for a database, which runs each statement in the current transaction if there is one.
        """
        return self.shared(lambda: self.persistence_manager_factory().get(database))

    def model_registry(self) -> ModelRegistry:
        """
        The models of the mapped classes, shared by every object manager. Override it to name properties another way:
        `ModelRegistry(property_names="as_written")`.
        """
        return self.shared(lambda: ModelRegistry())

    def graph_object_manager_factory(self) -> GraphObjectManagerFactory:
        return self.shared(
            lambda: GraphObjectManagerFactory(
                self.persistence_manager_factory(), self.model_registry(), self.index_advice()
            )
        )

    def index_advice(self) -> IndexAdvicePolicy:
        """
        What an ordered query with no range index to seek into does: WARN by default. Override it to return FAIL in
        development or CI.
        """
        return IndexAdvicePolicy.WARN

    def graph_object_manager(self, database: str = DEFAULT) -> StatelessGraphObjectManager:
        """
        The object manager for a graph database, which loads and saves fragments and views.
        """
        return self.shared(lambda: self.graph_object_manager_factory().get(database))

    def schema_options(self) -> SchemaOptions:
        """
        The schema catalogs `start` enforces. None by default: override it to hold indexes and constraints, say
        `SchemaOptions([SchemaCatalog.from_fragments(Person, Session, dimensions=lambda label, prop: 1536)])`.
        """
        return SchemaOptions()

    def schema_manager(self) -> SchemaManager:
        return self.shared(
            lambda: SchemaManager(self.registry(), self.graph_object_manager_factory(), self.schema_options())
        )

    async def start(self) -> None:
        """
        Enforces the schema catalogs. Entering the assembly, `async with DrivineAssembly() as drivine:`, calls it, as
        Spring starts drivine4j when its context is refreshed; otherwise, call it once, when the application starts. An
        application assembly that holds this one doesn't call it on entering: call it there. With no catalogs, or only
        empty ones, the manager says so, and nothing is applied. The databases are registered here, catalogs or not, as
        drivine4j registers them at start-up: so a missing `DATABASE_TYPE` raises `DrivineError` from `start`, not on
        first use.
        """
        await self.schema_manager().enforce()

    async def __aenter__(self) -> Self:
        """Starts Drivine. If starting fails, the assembly is closed before the error is raised."""
        try:
            await self.start()
        except BaseException:
            await self.aclose()
            raise
        return self

    def __enter__(self) -> NoReturn:
        """Refuses, as pi-pilgrim's does, and names the sync API, whose assembly `with` closes."""
        raise AsyncTeardownRequired(
            f"{type(self).__name__} closes its drivers and pools asynchronously: use async with, or "
            f"drivine.sync.DrivineAssembly for the sync API."
        )
