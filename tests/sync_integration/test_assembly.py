# Written by scripts/unasync.py from tests/integration/test_assembly.py:
# change that, and run the script again.
from pipilgrim import Assembled, AssemblyHolder

from drivine.sync import DatabaseRegistry, DrivineAssembly, PersistenceManager, QuerySpecification


class Routes:
    def __init__(self, pm: PersistenceManager) -> None:
        self.pm = pm


class Handler:
    pm: PersistenceManager = Assembled(assembly=DrivineAssembly)


def test_provides_one_manager_for_each_database():
    assembly = DrivineAssembly()
    try:
        assert assembly.persistence_manager() is assembly.persistence_manager("default")
        assert assembly.persistence_manager().get_one(QuerySpecification("RETURN 1")) == 1
    finally:
        assembly.close()


def test_injects_into_classes_made_elsewhere():
    AssemblyHolder.reset()
    try:
        handler = Handler()
        assert handler.pm is AssemblyHolder.shared(DrivineAssembly).persistence_manager()
    finally:
        AssemblyHolder.shared(DrivineAssembly).close()
        AssemblyHolder.reset()


def test_a_subclass_registers_its_own_databases():
    class MemgraphOnly(DrivineAssembly):
        def register_databases(self, registry: DatabaseRegistry) -> None:
            registry.build_or_resolve_from_env("memgraph")

    assembly = MemgraphOnly()
    try:
        assert assembly.persistence_manager().database == "default"
        assert assembly.registry().connection_provider() is assembly.registry().registered("memgraph")
        assert Routes(assembly.persistence_manager()).pm.get_one(QuerySpecification("RETURN 2")) == 2
    finally:
        assembly.close()


def test_closing_an_unused_assembly_does_nothing():
    DrivineAssembly().close()


def test_provides_one_object_manager_for_each_database():
    from drivine.sync import StatelessGraphObjectManager

    assembly = DrivineAssembly()
    try:
        gom = assembly.graph_object_manager()
        assert isinstance(gom, StatelessGraphObjectManager)
        assert gom is assembly.graph_object_manager("default")
        assert gom.registry is assembly.graph_object_manager_factory().get("default").registry
    finally:
        assembly.close()


def test_start_enforces_the_schema():
    import uuid

    from drivine.sync import SchemaCatalog, SchemaOptions, range_index

    label = f"T{uuid.uuid4().hex[:10]}"

    class WithSchema(DrivineAssembly):
        def schema_options(self) -> SchemaOptions:
            return SchemaOptions([SchemaCatalog.of(range_index(label, "a")).named(label)])

    assembly = WithSchema()
    try:
        assembly.start()
        gom = assembly.graph_object_manager()
        assert [it.properties for it in gom.indexes.list("RANGE") if it.label == label] == [("a",)]
        gom.indexes.drop(range_index(label, "a"))
    finally:
        assembly.close()
