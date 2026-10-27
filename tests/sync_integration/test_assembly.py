# Written by scripts/unasync.py from tests/integration/test_assembly.py:
# change that, and run the script again.
import pytest
from pipilgrim import Assembled, AssemblyHolder, PilgrimAssembly, PilgrimError

from drivine.sync import DatabaseRegistry, DrivineAssembly, PersistenceManager, QuerySpecification


class Routes:
    def __init__(self, pm: PersistenceManager) -> None:
        self.pm = pm


class Handler:
    pm: PersistenceManager = Assembled(assembly=DrivineAssembly)


def test_provides_one_manager_for_each_database():
    with DrivineAssembly() as assembly:
        assert assembly.persistence_manager() is assembly.persistence_manager("default")
        assert assembly.persistence_manager().get_one(QuerySpecification("RETURN 1")) == 1


def test_closes_the_drivers_on_the_way_out():
    with DrivineAssembly() as assembly:
        registry = assembly.registry()
        assert assembly.persistence_manager().get_one(QuerySpecification("RETURN 1")) == 1
    assert registry.providers == []
    with pytest.raises(PilgrimError, match="is closed"):
        assembly.persistence_manager()


def test_injects_into_classes_made_elsewhere():
    AssemblyHolder.reset()
    try:
        handler = Handler()
        assert handler.pm is AssemblyHolder.shared(DrivineAssembly).persistence_manager()
    finally:
        AssemblyHolder.close()
        AssemblyHolder.reset()


def test_an_application_assembly_closes_the_drivine_it_holds():
    class App(PilgrimAssembly):
        def __init__(self) -> None:
            self.drivine = AssemblyHolder.shared(DrivineAssembly)
            super().__init__()

        def routes(self) -> Routes:
            return self.shared(lambda: Routes(self.drivine.persistence_manager()))

    AssemblyHolder.reset()
    try:
        with App() as app:
            app.drivine.start()
            registry = app.drivine.registry()
            assert app.routes().pm.get_one(QuerySpecification("RETURN 3")) == 3
        assert registry.providers == []
    finally:
        AssemblyHolder.reset()


def test_a_subclass_registers_its_own_databases():
    class MemgraphOnly(DrivineAssembly):
        def register_databases(self, registry: DatabaseRegistry) -> None:
            registry.build_or_resolve_from_env("memgraph")

    with MemgraphOnly() as assembly:
        assert assembly.persistence_manager().database == "default"
        assert assembly.registry().connection_provider() is assembly.registry().registered("memgraph")
        assert Routes(assembly.persistence_manager()).pm.get_one(QuerySpecification("RETURN 2")) == 2


def test_closing_an_unused_assembly_does_nothing():
    DrivineAssembly().close()


def test_provides_one_object_manager_for_each_database():
    from drivine.sync import StatelessGraphObjectManager

    with DrivineAssembly() as assembly:
        gom = assembly.graph_object_manager()
        assert isinstance(gom, StatelessGraphObjectManager)
        assert gom is assembly.graph_object_manager("default")
        assert gom.registry is assembly.graph_object_manager_factory().get("default").registry


def test_entering_enforces_the_schema():
    import uuid

    from drivine.sync import SchemaCatalog, SchemaOptions, range_index

    label = f"T{uuid.uuid4().hex[:10]}"

    class WithSchema(DrivineAssembly):
        def schema_options(self) -> SchemaOptions:
            return SchemaOptions([SchemaCatalog.of(range_index(label, "a")).named(label)])

    with WithSchema() as assembly:
        gom = assembly.graph_object_manager()
        assert [it.properties for it in gom.indexes.list("RANGE") if it.label == label] == [("a",)]
        gom.indexes.drop(range_index(label, "a"))


def test_a_registry_closes_on_the_way_out():
    with DatabaseRegistry() as registry:
        provider = registry.build_or_resolve_from_env()
        assert provider is registry.connection_provider()
    assert registry.providers == []
