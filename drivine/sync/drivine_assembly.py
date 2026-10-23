# Written by scripts/unasync.py from drivine/drivine_assembly.py:
# change that, and run the script again.
from __future__ import annotations

try:
    from pipilgrim import PilgrimAssembly
except ModuleNotFoundError as e:
    raise ModuleNotFoundError("DrivineAssembly needs pi-pilgrim: pip install 'drivine[pilgrim]'", name=e.name) from e

from drivine.dsl.index_advice_policy import IndexAdvicePolicy
from drivine.model.model_registry import ModelRegistry
from drivine.sync.connection.database_registry import DEFAULT, DatabaseRegistry
from drivine.sync.manager.graph_object_manager_factory import GraphObjectManagerFactory
from drivine.sync.manager.persistence_manager import PersistenceManager
from drivine.sync.manager.persistence_manager_factory import PersistenceManagerFactory
from drivine.sync.manager.stateless_graph_object_manager import StatelessGraphObjectManager
from drivine.sync.schema.schema_manager import SchemaManager
from drivine.sync.schema.schema_options import SchemaOptions


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
        self.make_injectable(self.graph_object_manager, by_type=StatelessGraphObjectManager)
        self.make_injectable(self.model_registry, by_type=ModelRegistry)

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

    def start(self) -> None:
        """
        Enforces the schema catalogs. Call it once, when the application starts. With none, or only empty ones, the
        manager says so, and nothing is applied.
        """
        self.schema_manager().enforce()

    def close(self) -> None:
        """
        Closes every database's driver or pool. Call it once, when the application stops.
        """
        if self._registry_made:
            self.registry().close()

    def _registered(self, registry: DatabaseRegistry) -> DatabaseRegistry:
        self.register_databases(registry)
        self._registry_made = True
        return registry
