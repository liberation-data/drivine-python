from pipilgrim import Assembled, AssemblyHolder

from drivine import DatabaseRegistry, DrivineAssembly, PersistenceManager, QuerySpecification


class Routes:
    def __init__(self, pm: PersistenceManager) -> None:
        self.pm = pm


class Handler:
    pm: PersistenceManager = Assembled(assembly=DrivineAssembly)


async def test_provides_one_manager_for_each_database():
    assembly = DrivineAssembly()
    try:
        assert assembly.persistence_manager() is assembly.persistence_manager("default")
        assert await assembly.persistence_manager().get_one(QuerySpecification("RETURN 1")) == 1
    finally:
        await assembly.close()


async def test_injects_into_classes_made_elsewhere():
    AssemblyHolder.reset()
    try:
        handler = Handler()
        assert handler.pm is AssemblyHolder.shared(DrivineAssembly).persistence_manager()
    finally:
        await AssemblyHolder.shared(DrivineAssembly).close()
        AssemblyHolder.reset()


async def test_a_subclass_registers_its_own_databases():
    class MemgraphOnly(DrivineAssembly):
        def register_databases(self, registry: DatabaseRegistry) -> None:
            registry.build_or_resolve_from_env("memgraph")

    assembly = MemgraphOnly()
    try:
        assert assembly.persistence_manager().database == "default"
        assert assembly.registry().connection_provider() is assembly.registry().registered("memgraph")
        assert await Routes(assembly.persistence_manager()).pm.get_one(QuerySpecification("RETURN 2")) == 2
    finally:
        await assembly.close()


async def test_closing_an_unused_assembly_does_nothing():
    await DrivineAssembly().close()


async def test_provides_one_object_manager_for_each_database():
    from drivine import StatelessGraphObjectManager

    assembly = DrivineAssembly()
    try:
        gom = assembly.graph_object_manager()
        assert isinstance(gom, StatelessGraphObjectManager)
        assert gom is assembly.graph_object_manager("default")
        assert gom.registry is assembly.graph_object_manager_factory().get("default").registry
    finally:
        await assembly.close()


async def test_start_enforces_the_schema():
    import uuid

    from drivine import SchemaCatalog, SchemaOptions, range_index

    label = f"T{uuid.uuid4().hex[:10]}"

    class WithSchema(DrivineAssembly):
        def schema_options(self) -> SchemaOptions:
            return SchemaOptions([SchemaCatalog.of(range_index(label, "a")).named(label)])

    assembly = WithSchema()
    try:
        await assembly.start()
        gom = assembly.graph_object_manager()
        assert [it.properties for it in await gom.indexes.list("RANGE") if it.label == label] == [("a",)]
        await gom.indexes.drop(range_index(label, "a"))
    finally:
        await assembly.close()
