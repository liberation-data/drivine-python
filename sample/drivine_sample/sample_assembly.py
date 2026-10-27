from __future__ import annotations

from drivine import (
    DatabaseRegistry,
    IndexAdvicePolicy,
    PersistenceManager,
    SchemaOptions,
    StatelessGraphObjectManager,
)
from drivine.connection.database_registry import DEFAULT
from drivine.drivine_assembly import DrivineAssembly


class SampleAssembly(DrivineAssembly):
    """
    The sample application, as drivine4j's SampleAppContext wires Spring and TypeScript's SampleAppModule wires Nest:
    Drivine over one database, with that database's object manager and persistence manager.

        async with SampleAssembly("memgraph") as assembly:   # MEMGRAPH_DATABASE_*; "default" for DATABASE_*
            gom = assembly.sample_graph_object_manager()
            ...

    Entering it holds the schema, if it was given one, and leaving it closes the driver.

    `schema` and `index_advice` are what an application would configure: the catalogs `start` enforces, and what an
    ordered query with no range index does.
    """

    def __init__(
        self,
        database: str = DEFAULT,
        *,
        schema: SchemaOptions | None = None,
        index_advice: IndexAdvicePolicy = IndexAdvicePolicy.WARN,
    ) -> None:
        self.database = database
        self._schema = schema or SchemaOptions()
        self._index_advice = index_advice
        super().__init__()

    def register_databases(self, registry: DatabaseRegistry) -> None:
        registry.build_or_resolve_from_env(None if self.database == DEFAULT else self.database)

    def schema_options(self) -> SchemaOptions:
        return self._schema

    def index_advice(self) -> IndexAdvicePolicy:
        return self._index_advice

    def sample_graph_object_manager(self) -> StatelessGraphObjectManager:
        return self.graph_object_manager(self.database)

    def sample_persistence_manager(self) -> PersistenceManager:
        return self.persistence_manager(self.database)
