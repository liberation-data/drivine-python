# Written by scripts/unasync.py from tests/integration/test_transactions.py:
# change that, and run the script again.

import pytest

from drivine.sync import (
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


def names(pm: PersistenceManager) -> list[str]:
    return pm.query(QuerySpecification[str]("MATCH (p:Person) RETURN p.name ORDER BY p.name"))


class People:
    def __init__(self, pm: PersistenceManager) -> None:
        self.pm = pm

    @transactional()
    def add_both(self, first: str, second: str) -> None:
        self.pm.execute(create(first))
        self.add(second)

    @transactional()
    def add(self, name: str) -> None:
        self.pm.execute(create(name))

    @transactional()
    def add_then_fail(self, name: str) -> None:
        self.pm.execute(create(name))
        raise ValueError("after the write")

    @transactional()
    def add_and_swallow(self, name: str) -> None:
        self.pm.execute(create(name))
        try:
            self.add_then_fail("inner")
        except ValueError:
            pass


def test_commits(pm: PersistenceManager):
    People(pm).add_both("Ada", "Grace")
    assert names(pm) == ["Ada", "Grace"]


@pytest.mark.rolls_back
def test_sees_its_own_writes_before_it_commits(pm: PersistenceManager):
    with transaction(rollback=True):
        pm.execute(create("Ada"))
        assert names(pm) == ["Ada"]
    assert names(pm) == []


@pytest.mark.rolls_back
def test_rolls_back_on_an_exception(pm: PersistenceManager):
    with pytest.raises(ValueError):
        People(pm).add_then_fail("Ada")
    assert names(pm) == []


@pytest.mark.rolls_back
def test_rolls_back_an_inner_failure_the_caller_caught(pm: PersistenceManager):
    with pytest.raises(TransactionRolledBackError):
        People(pm).add_and_swallow("Ada")
    assert names(pm) == []


def test_spans_databases(factory: PersistenceManagerFactory):
    neo4j, memgraph = factory.get("default"), factory.get("memgraph")
    for it in (neo4j, memgraph):
        it.execute(QuerySpecification("MATCH (n) DETACH DELETE n"))

    with pytest.raises(ValueError):
        with transaction():
            neo4j.execute(create("in neo4j"))
            memgraph.execute(create("in memgraph"))
            raise ValueError("roll both back")
    assert names(neo4j) == [] and names(memgraph) == []

    with transaction():
        neo4j.execute(create("in neo4j"))
        memgraph.execute(create("in memgraph"))
    assert names(neo4j) == ["in neo4j"]
    assert names(memgraph) == ["in memgraph"]
