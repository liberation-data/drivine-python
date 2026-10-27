"""
DrivineAssembly closes the registry's drivers and pools when it closes, through pi-pilgrim's lifecycle, as Spring
closes drivine4j's with its context; so does a DatabaseRegistry that was entered. Async and sync alike.
"""

from collections.abc import Iterator

import pytest
from pipilgrim import AssemblyHolder, PilgrimAssembly, PilgrimError

from drivine import DatabaseRegistry, DatabaseType, DrivineAssembly
from drivine.sync import ConnectionProvider as SyncConnectionProvider
from drivine.sync import DatabaseRegistry as SyncDatabaseRegistry
from drivine.sync import DrivineAssembly as SyncDrivineAssembly
from tests.unit.fakes import FakeProvider


class EndingProvider(FakeProvider):
    """Says when it is ended."""

    def __init__(self, name: str = "default") -> None:
        super().__init__(name)
        self.ended = 0

    async def end(self) -> None:
        self.ended += 1


class SyncEndingProvider(SyncConnectionProvider):
    def __init__(self, name: str = "default") -> None:
        self.name = name
        self.type = DatabaseType.NEO4J
        self.ended = 0

    def connect(self):  # type: ignore[override]
        raise NotImplementedError

    def end(self) -> None:
        self.ended += 1


class FakeDrivine(DrivineAssembly):
    def __init__(self) -> None:
        self.provider = EndingProvider()
        self.started = 0
        super().__init__()

    def register_databases(self, registry: DatabaseRegistry) -> None:
        registry.register(self.provider)

    async def start(self) -> None:
        self.started += 1
        await super().start()


class SyncFakeDrivine(SyncDrivineAssembly):
    def __init__(self) -> None:
        self.provider = SyncEndingProvider()
        self.started = 0
        super().__init__()

    def register_databases(self, registry: SyncDatabaseRegistry) -> None:
        registry.register(self.provider)

    def start(self) -> None:
        self.started += 1
        super().start()


@pytest.fixture(autouse=True)
def holder() -> Iterator[None]:
    AssemblyHolder.reset()
    yield
    AssemblyHolder.reset()


async def test_async_with_starts_drivine_and_closes_its_providers():
    async with FakeDrivine() as drivine:
        assert drivine.started == 1
        assert drivine.persistence_manager() is drivine.persistence_manager("default")
        assert drivine.provider.ended == 0
    assert drivine.provider.ended == 1
    with pytest.raises(PilgrimError, match="is closed"):
        drivine.persistence_manager()


def test_with_starts_the_sync_assembly_and_closes_its_providers():
    with SyncFakeDrivine() as drivine:
        assert drivine.started == 1
        drivine.persistence_manager()
        assert drivine.provider.ended == 0
    assert drivine.provider.ended == 1
    with pytest.raises(PilgrimError, match="is closed"):
        drivine.graph_object_manager()


async def test_closing_twice_ends_the_providers_once():
    drivine = FakeDrivine()
    drivine.persistence_manager()
    await drivine.aclose()
    await drivine.aclose()
    assert drivine.provider.ended == 1


async def test_closing_an_unused_assembly_makes_no_registry():
    drivine = FakeDrivine()
    await drivine.aclose()
    sync = SyncFakeDrivine()
    sync.close()
    assert (drivine.provider.ended, sync.provider.ended) == (0, 0)


async def test_a_failed_start_closes_the_assembly():
    class FailingStart(FakeDrivine):
        async def start(self) -> None:
            self.registry()
            raise RuntimeError("no schema")

    drivine = FailingStart()
    with pytest.raises(RuntimeError, match="no schema"):
        async with drivine:
            pytest.fail("the body ran")
    assert drivine.provider.ended == 1


def test_a_failed_start_closes_the_sync_assembly():
    class FailingStart(SyncFakeDrivine):
        def start(self) -> None:
            self.registry()
            raise RuntimeError("no schema")

    drivine = FailingStart()
    with pytest.raises(RuntimeError, match="no schema"), drivine:
        pytest.fail("the body ran")
    assert drivine.provider.ended == 1


async def test_the_async_assembly_refuses_a_sync_close():
    drivine = FakeDrivine()
    drivine.registry()
    with pytest.raises(PilgrimError, match="aclose"):
        drivine.close()
    assert drivine.provider.ended == 0
    await drivine.aclose()
    assert drivine.provider.ended == 1


class App(PilgrimAssembly):
    def __init__(self) -> None:
        self.drivine = AssemblyHolder.shared(FakeDrivine)
        super().__init__()

    def greeter(self) -> object:
        return self.shared(lambda: (self.drivine.persistence_manager(), "hello"))


class SyncApp(PilgrimAssembly):
    def __init__(self) -> None:
        self.drivine = AssemblyHolder.shared(SyncFakeDrivine)
        super().__init__()

    def greeter(self) -> object:
        return self.shared(lambda: (self.drivine.persistence_manager(), "hello"))


async def test_an_application_assembly_closes_the_drivine_it_holds():
    async with App() as app:
        await app.drivine.start()
        app.greeter()
    assert app.drivine.provider.ended == 1
    # AssemblyHolder forgets a closed assembly, so the next application gets a new one.
    assert AssemblyHolder.shared(FakeDrivine) is not app.drivine
    with pytest.raises(PilgrimError, match="is closed"):
        app.drivine.persistence_manager()


def test_a_sync_application_assembly_closes_the_drivine_it_holds():
    with SyncApp() as app:
        app.greeter()
    assert app.drivine.provider.ended == 1
    with pytest.raises(PilgrimError, match="is closed"):
        app.greeter()


async def test_async_with_closes_a_registry():
    provider = EndingProvider()
    async with DatabaseRegistry() as registry:
        registry.register(provider)
        assert registry.connection_provider() is provider
    assert provider.ended == 1
    assert registry.providers == []


def test_with_closes_a_sync_registry():
    provider = SyncEndingProvider()
    with SyncDatabaseRegistry() as registry:
        registry.register(provider)
    assert provider.ended == 1
    assert registry.providers == []


async def test_a_registry_closes_when_its_block_raises():
    provider = EndingProvider()
    with pytest.raises(RuntimeError):
        async with DatabaseRegistry() as registry:
            registry.register(provider)
            raise RuntimeError
    assert provider.ended == 1
