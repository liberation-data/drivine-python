"""
DrivineAssembly closes the registry's drivers and pools when it closes, through pi-pilgrim's lifecycle, as Spring
closes drivine4j's with its context; so does a DatabaseRegistry that was entered. Async and sync alike.
"""

import asyncio
import gc
import inspect
from collections.abc import Iterator

import pytest
from pipilgrim import AssemblyHolder, PilgrimAssembly, PilgrimError

from drivine import DatabaseRegistry, DatabaseType, DrivineAssembly
from drivine.drivine_assembly import _ASYNC_TEARDOWN_REFUSAL
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


async def test_await_close_closes_the_providers_as_in_0_2_0():
    drivine = FakeDrivine()
    drivine.persistence_manager()
    await drivine.close()
    assert drivine.provider.ended == 1
    with pytest.raises(PilgrimError, match="is closed"):
        drivine.persistence_manager()


async def test_await_close_twice_ends_the_providers_once():
    drivine = FakeDrivine()
    drivine.registry()
    await drivine.close()
    await drivine.close()
    await drivine.aclose()
    assert drivine.provider.ended == 1


async def test_await_close_without_a_registry_closes_the_assembly():
    drivine = FakeDrivine()
    await drivine.close()
    await drivine.close()
    assert drivine.provider.ended == 0
    with pytest.raises(PilgrimError, match="is closed"):
        drivine.registry()


async def test_await_close_after_aclose_does_nothing():
    drivine = FakeDrivine()
    drivine.registry()
    await drivine.aclose()
    assert drivine.provider.ended == 1
    await drivine.close()
    assert drivine.provider.ended == 1


@pytest.mark.parametrize("registry", [False, True], ids=["no registry", "registry"])
def test_asyncio_run_takes_what_close_returns(registry: bool):
    drivine = FakeDrivine()
    if registry:
        drivine.registry()
    asyncio.run(drivine.close())
    assert drivine.provider.ended == (1 if registry else 0)
    with pytest.raises(PilgrimError, match="is closed"):
        drivine.registry()


def test_what_close_returns_once_closed_is_a_finished_coroutine():
    closed = FakeDrivine().close()
    assert asyncio.iscoroutine(closed) and not inspect.iscoroutine(closed)
    with pytest.raises(StopIteration):
        closed.send(None)
    with pytest.raises(ValueError, match="thrown"):
        closed.throw(ValueError("thrown"))
    with pytest.raises(ValueError):
        closed.throw(ValueError)
    with pytest.raises(KeyError, match="value"):
        closed.throw(KeyError, KeyError("value"))
    closed.close()
    closed.close()


def test_close_not_awaited_leaves_the_providers_open_and_python_warns():
    drivine = FakeDrivine()
    drivine.registry()
    with pytest.warns(RuntimeWarning, match="never awaited"):
        drivine.close()
        gc.collect()
    assert drivine.provider.ended == 0
    asyncio.run(drivine.aclose())
    assert drivine.provider.ended == 1


def test_pi_pilgrim_still_refuses_an_async_teardown_with_the_text_close_matches():
    # close() tells this refusal from others by its text. If pi-pilgrim rewords it, close() would raise it again, as
    # 0.3.0 did (#228): this fails first.
    class AsyncTeardown(PilgrimAssembly):
        def registry(self) -> DatabaseRegistry:
            return self.shared(DatabaseRegistry)

    assembly = AsyncTeardown()
    assembly.registry()
    with pytest.raises(PilgrimError) as refused:
        assembly.close()
    assert _ASYNC_TEARDOWN_REFUSAL in str(refused.value)
    asyncio.run(assembly.aclose())


def test_a_sync_close_of_an_assembly_holding_drivine_as_a_component_fails_but_closes_it():
    # pi-pilgrim's sync close() refuses what a component's close() returns when it is awaitable, as DrivineAssembly's
    # is. Hold Drivine with AssemblyHolder.shared, as App does, or close the holder with await aclose().
    class Holder(PilgrimAssembly):
        def drivine(self) -> FakeDrivine:
            return self.shared(FakeDrivine)

    holder = Holder()
    drivine = holder.drivine()
    with pytest.raises(ExceptionGroup) as failed:
        holder.close()
    assert "await aclose()" in str(failed.value.exceptions[0])
    with pytest.raises(PilgrimError, match="is closed"):
        drivine.registry()


def test_the_sync_assembly_closes_with_close():
    drivine = SyncFakeDrivine()
    drivine.registry()
    assert drivine.close() is None
    drivine.close()
    assert drivine.provider.ended == 1


async def test_the_async_assembly_refuses_with():
    drivine = FakeDrivine()
    with pytest.raises(PilgrimError, match="async with"):
        with drivine:  # type: ignore[misc]
            pytest.fail("the body ran")
    with pytest.raises(PilgrimError, match="async with"):
        drivine.__exit__(None, None, None)
    assert drivine.started == 0
    await drivine.aclose()


async def test_close_raises_a_refusal_that_is_not_about_an_async_teardown():
    # Closing from inside a factory is refused for another reason: close() raises it, not aclose() to await later.
    class ClosesWhileMaking(FakeDrivine):
        def closer(self) -> object:
            return self.shared(lambda: self.close())

    drivine = ClosesWhileMaking()
    drivine.registry()
    with pytest.raises(PilgrimError, match="while it is making a component"):
        drivine.closer()
    assert drivine.provider.ended == 0
    await drivine.close()
    assert drivine.provider.ended == 1


async def test_await_close_ends_every_provider_when_one_fails():
    class FailingProvider(EndingProvider):
        async def end(self) -> None:
            await super().end()
            raise RuntimeError("pool won't close")

    class TwoDatabases(FakeDrivine):
        def __init__(self) -> None:
            self.failing = FailingProvider("failing")
            super().__init__()

        def register_databases(self, registry: DatabaseRegistry) -> None:
            registry.register(self.failing)
            super().register_databases(registry)

    drivine = TwoDatabases()
    drivine.registry()
    await drivine.close()
    assert (drivine.failing.ended, drivine.provider.ended) == (1, 1)
    with pytest.raises(PilgrimError, match="is closed"):
        drivine.registry()


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


async def test_await_close_on_an_application_assemblys_drivine_closes_it_as_in_0_2_0():
    # 0.2.0's README: `self.drivine = AssemblyHolder.shared(DrivineAssembly)`, then `await assembly.drivine.close()`.
    app = App()
    app.greeter()
    await app.drivine.close()
    assert app.drivine.provider.ended == 1
    await app.aclose()
    assert app.drivine.provider.ended == 1


def test_a_sync_close_of_an_application_assembly_closes_a_drivine_with_no_registry():
    # The application assembly's close() calls the DrivineAssembly's without awaiting it: nothing is left to await.
    app = App()
    drivine = app.drivine
    app.close()
    with pytest.raises(PilgrimError, match="is closed"):
        drivine.registry()


def test_a_sync_close_of_an_application_assembly_refuses_once_drivine_has_a_registry():
    app = App()
    app.greeter()
    with pytest.raises(PilgrimError, match="aclose"):
        app.close()
    assert app.drivine.provider.ended == 0


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
