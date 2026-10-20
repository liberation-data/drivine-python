# Written by scripts/unasync.py from tests/integration/test_falkordb.py:
# change that, and run the script again.
"""What FalkorDB, which commits each statement as it runs, does with transactions and batches."""

import logging
from collections.abc import Iterator

import pytest

from drivine.connection.connection_properties import ConnectionProperties
from drivine.manager.non_atomic_batch_error import NonAtomicBatchError
from drivine.sync import (
    DatabaseRegistry,
    DrivineError,
    PersistenceManagerFactory,
    QuerySpecification,
    StatelessGraphObjectManager,
    TransactionMode,
    transaction,
)
from tests.models import Person


@pytest.fixture
def strict() -> Iterator[PersistenceManagerFactory]:
    registry = DatabaseRegistry()
    properties = ConnectionProperties.from_env("falkordb")
    registry.builder().with_properties(properties).transaction_mode(TransactionMode.STRICT).register("strict")
    yield PersistenceManagerFactory(registry)
    registry.close()


def test_warn_runs_a_transaction_statement_by_statement(factory, caplog):
    pm = factory.get("falkordb")
    pm.execute(QuerySpecification("MATCH (n) DETACH DELETE n"))
    with caplog.at_level(logging.WARNING), pytest.raises(ValueError):
        with transaction():
            pm.execute(QuerySpecification("CREATE (:Person {name: 'kept'})"))
            raise ValueError
    assert pm.get_one(QuerySpecification("MATCH (p:Person) RETURN count(p)")) == 1
    assert "Nothing was rolled back" in caplog.text


def test_strict_refuses_a_transaction(strict):
    pm = strict.get("strict")
    with pytest.raises(DrivineError, match="does not support multi-statement transactions"):
        with transaction():
            pm.execute(QuerySpecification("RETURN 1"))


def test_a_batch_warns_or_is_refused(factory, strict, caplog):
    people = [Person(uuid="p1", name="Ada"), Person(uuid="p2", name="Grace")]
    from tests.models import PersonCareer

    batch = [*people, PersonCareer(person=Person(uuid="p3", name="Alan"))]
    with pytest.raises(NonAtomicBatchError):
        StatelessGraphObjectManager(strict.get("strict")).save_all(batch)
    gom = StatelessGraphObjectManager(factory.get("falkordb"))
    gom.persistence_manager.execute(QuerySpecification("MATCH (n) DETACH DELETE n"))
    with caplog.at_level(logging.WARNING):
        gom.save_all(batch)
    assert "runs statement by statement on FalkorDB" in caplog.text
    assert gom.count(Person) == 3


def test_positional_parameters_and_dates(factory):
    from datetime import UTC, datetime

    pm = factory.get("falkordb")
    assert pm.get_one(QuerySpecification("RETURN $1 + $2").bind([1, 2])) == 3
    moment = datetime(2040, 1, 1, 12, 30, tzinfo=UTC)
    assert pm.get_one(QuerySpecification("RETURN $at").bind({"at": moment})) == "2040-01-01T12:30:00.000000000Z"
