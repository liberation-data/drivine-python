import asyncio

import pytest

from drivine import (
    DrivineError,
    PersistenceManager,
    PersistenceManagerFactory,
    QuerySpecification,
    TransactionRolledBackError,
    transaction,
    transactional,
)

# Written in Cypher, which Postgres and Oracle compile to their SQL.
pytestmark = pytest.mark.written_in_cypher


def create(name: str) -> QuerySpecification[None]:
    return QuerySpecification("CREATE (:Person {name: $name})").bind({"name": name})


async def names(pm: PersistenceManager) -> list[str]:
    return await pm.query(QuerySpecification[str]("MATCH (p:Person) RETURN p.name ORDER BY p.name"))


class People:
    def __init__(self, pm: PersistenceManager) -> None:
        self.pm = pm

    @transactional()
    async def add_both(self, first: str, second: str) -> None:
        await self.pm.execute(create(first))
        await self.add(second)

    @transactional()
    async def add(self, name: str) -> None:
        await self.pm.execute(create(name))

    @transactional()
    async def add_then_fail(self, name: str) -> None:
        await self.pm.execute(create(name))
        raise ValueError("after the write")

    @transactional()
    async def add_and_swallow(self, name: str) -> None:
        await self.pm.execute(create(name))
        try:
            await self.add_then_fail("inner")
        except ValueError:
            pass


async def test_commits(pm: PersistenceManager):
    await People(pm).add_both("Ada", "Grace")
    assert await names(pm) == ["Ada", "Grace"]


@pytest.mark.rolls_back
async def test_sees_its_own_writes_before_it_commits(pm: PersistenceManager):
    async with transaction(rollback=True):
        await pm.execute(create("Ada"))
        assert await names(pm) == ["Ada"]
    assert await names(pm) == []


@pytest.mark.rolls_back
async def test_rolls_back_on_an_exception(pm: PersistenceManager):
    with pytest.raises(ValueError):
        await People(pm).add_then_fail("Ada")
    assert await names(pm) == []


@pytest.mark.rolls_back
async def test_rolls_back_an_inner_failure_the_caller_caught(pm: PersistenceManager):
    with pytest.raises(TransactionRolledBackError):
        await People(pm).add_and_swallow("Ada")
    assert await names(pm) == []


@pytest.mark.async_only
async def test_concurrent_tasks_share_the_transaction(pm: PersistenceManager):
    async with transaction():
        await asyncio.gather(*(pm.execute(create(f"p{it:02}")) for it in range(20)))
    assert await names(pm) == [f"p{it:02}" for it in range(20)]


@pytest.mark.async_only
@pytest.mark.rolls_back
async def test_a_failed_statement_among_concurrent_tasks_rolls_all_back(pm: PersistenceManager):
    async def tolerant(spec: QuerySpecification[None]) -> None:
        try:
            await pm.execute(spec)
        except DrivineError:
            pass

    with pytest.raises(TransactionRolledBackError):
        async with transaction():
            await asyncio.gather(
                tolerant(create("a")), tolerant(QuerySpecification("MATCH (n RETURN n")), tolerant(create("b"))
            )
    assert await names(pm) == []


async def test_spans_databases(factory: PersistenceManagerFactory):
    neo4j, memgraph = factory.get("default"), factory.get("memgraph")
    for it in (neo4j, memgraph):
        await it.execute(QuerySpecification("MATCH (n) DETACH DELETE n"))

    with pytest.raises(ValueError):
        async with transaction():
            await neo4j.execute(create("in neo4j"))
            await memgraph.execute(create("in memgraph"))
            raise ValueError("roll both back")
    assert await names(neo4j) == [] and await names(memgraph) == []

    async with transaction():
        await neo4j.execute(create("in neo4j"))
        await memgraph.execute(create("in memgraph"))
    assert await names(neo4j) == ["in neo4j"]
    assert await names(memgraph) == ["in memgraph"]
