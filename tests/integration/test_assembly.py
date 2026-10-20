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
