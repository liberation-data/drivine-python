import asyncio

import pytest

from drivine import (
    DrivineError,
    QuerySpecification,
    TransactionalPersistenceManager,
    TransactionRolledBackError,
    current_transaction,
    transaction,
    transactional,
)
from drivine.manager.delegating_persistence_manager import DelegatingPersistenceManager
from drivine.manager.non_transactional_persistence_manager import NonTransactionalPersistenceManager
from tests.unit.fakes import FakeProvider


def spec(text: str) -> QuerySpecification[str]:
    return QuerySpecification(text)


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def pm(provider: FakeProvider) -> DelegatingPersistenceManager:
    return DelegatingPersistenceManager(
        TransactionalPersistenceManager(provider, "default"), NonTransactionalPersistenceManager(provider, "default")
    )


class Service:
    def __init__(self, pm: DelegatingPersistenceManager) -> None:
        self.pm = pm

    @transactional()
    async def outer(self) -> str:
        await self.pm.query(spec("a"))
        await self.inner()
        return "done"

    @transactional
    async def inner(self) -> None:
        await self.pm.query(spec("b"))

    @transactional()
    async def outer_catching(self) -> None:
        await self.pm.query(spec("a"))
        try:
            await self.failing()
        except ValueError:
            pass

    @transactional()
    async def failing(self) -> None:
        await self.pm.query(spec("b"))
        raise ValueError("inner")


class TestOutsideATransaction:
    async def test_each_statement_has_its_own_connection(self, pm, provider):
        await pm.query(spec("a"))
        await pm.query(spec("b"))
        assert provider.events == ["default:start:a", "default:end:a", "default:release"] * 1 + [
            "default:start:b",
            "default:end:b",
            "default:release",
        ]
        assert provider.connections == 2

    async def test_the_transactional_manager_refuses(self, provider):
        with pytest.raises(DrivineError, match="requires a transaction"):
            await TransactionalPersistenceManager(provider, "default").query(spec("a"))

    async def test_a_failed_statement_is_a_drivine_error_with_its_cause(self, pm):
        with pytest.raises(DrivineError) as raised:
            await pm.query(spec("fail"))
        assert isinstance(raised.value.root_cause, RuntimeError)
        assert raised.value.spec is not None


class TestTransactional:
    async def test_commits_once_when_the_outermost_call_returns(self, pm, provider):
        assert await Service(pm).outer() == "done"
        assert provider.events == [
            "default:begin",
            "default:start:a",
            "default:end:a",
            "default:start:b",
            "default:end:b",
            "default:commit",
            "default:release",
        ]
        assert current_transaction() is None

    async def test_rolls_back_when_an_exception_leaves_it(self, pm, provider):
        with pytest.raises(ValueError):
            await Service(pm).failing()
        assert provider.events[-2:] == ["default:rollback", "default:release"]
        assert current_transaction() is None

    async def test_an_inner_failure_the_caller_catches_still_rolls_back(self, pm, provider):
        with pytest.raises(TransactionRolledBackError) as raised:
            await Service(pm).outer_catching()
        assert isinstance(raised.value.__cause__, ValueError)
        assert "default:commit" not in provider.events
        assert provider.events[-2:] == ["default:rollback", "default:release"]

    async def test_no_rollback_for_commits_though_the_exception_leaves(self, pm, provider):
        @transactional(no_rollback_for=LookupError)
        async def lookup() -> None:
            await pm.query(spec("a"))
            raise KeyError("missing")

        with pytest.raises(KeyError):
            await lookup()
        assert provider.events[-2:] == ["default:commit", "default:release"]

    async def test_rollback_true_rolls_back_a_success(self, pm, provider):
        async with transaction(rollback=True):
            await pm.query(spec("a"))
        assert provider.events[-2:] == ["default:rollback", "default:release"]

    async def test_a_transaction_that_runs_no_statement_opens_no_connection(self, provider):
        @transactional()
        async def without_drivine() -> int:
            return 42

        assert await without_drivine() == 42
        assert provider.connections == 0

    async def test_one_connection_for_each_database(self, provider):
        events: list[str] = []
        first, second = FakeProvider("first", events), FakeProvider("second", events)
        async with transaction():
            await TransactionalPersistenceManager(first, "first").query(spec("a"))
            await TransactionalPersistenceManager(second, "second").query(spec("b"))
            await TransactionalPersistenceManager(first, "first").query(spec("c"))
        assert first.connections == second.connections == 1
        assert [it for it in events if it.endswith(("commit", "begin"))] == [
            "first:begin",
            "second:begin",
            "first:commit",
            "second:commit",
        ]

    def test_refuses_a_sync_function(self):
        with pytest.raises(TypeError, match="takes an async function"):

            @transactional()
            def sync() -> None:
                pass

    async def test_a_block_cant_be_entered_twice_at_once(self):
        block = transaction()
        async with block:
            with pytest.raises(DrivineError, match="already running"):
                async with block:
                    pass


class TestConcurrentTasks:
    async def test_tasks_inside_a_transaction_share_it_one_statement_at_a_time(self, pm, provider):
        async with transaction() as tx:
            results = await asyncio.gather(*(pm.query(spec(f"slow{it}")) for it in range(5)))
            assert all(current_transaction() is tx for _ in results)
        assert provider.connections == 1
        starts = [it for it in provider.events if ":start:" in it]
        assert starts == [f"default:start:slow{it}" for it in range(5)]  # in the order they were issued
        for index in range(5):
            assert provider.events.index(f"default:end:slow{index}") < provider.events.index(
                f"default:start:slow{index + 1}" if index < 4 else "default:commit"
            )

    async def test_a_failed_task_makes_the_transaction_rollback_only(self, pm, provider):
        async def tolerant(text: str) -> object:
            try:
                return await pm.query(spec(text))
            except DrivineError as error:
                return error

        with pytest.raises(TransactionRolledBackError):
            async with transaction():
                results = await asyncio.gather(tolerant("slow1"), tolerant("fail"), tolerant("slow2"))
        assert results[0] == ["slow1"]
        assert isinstance(results[1], DrivineError) and not isinstance(results[1], TransactionRolledBackError)
        assert isinstance(results[2], TransactionRolledBackError)  # failed at once, without running
        assert "default:start:slow2" not in provider.events
        assert provider.events[-2:] == ["default:rollback", "default:release"]

    async def test_a_task_that_outlives_its_transaction_cant_use_it(self, pm):
        async with transaction():
            started = asyncio.Event()

            async def late() -> None:
                started.set()
                await asyncio.sleep(0.02)
                await pm.query(spec("a"))

            task = asyncio.create_task(late())
            await started.wait()
        with pytest.raises(DrivineError, match="has ended"):
            await task
