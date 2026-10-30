"""
The sync API's SQL providers, as test_graph_tables_providers has the async ones: a block a deadlock turned away is run
again on the same connection, and a store that will not make the tables is not asked again on the pool (#287).
"""

import logging
from collections.abc import Sequence
from typing import Any

import pytest

from drivine.sync.connection.oracle.oracle_connection_provider import OracleConnectionProvider
from drivine.sync.connection.postgres.postgres_connection_provider import PostgresConnectionProvider
from drivine.sync.manager import contention
from drivine.sync.manager.contention import CONTENDED_ATTEMPTS
from drivine.sync.schema.schema_executor import SchemaExecutor

DEADLOCK = "ORA-04020: deadlock detected while trying to lock object DRIVINE.DRIVINE_NODE"
BUSY = "ORA-06512: at line 22\nORA-00054: resource busy and acquire with NOWAIT specified or timeout expired"


class PostgresError(Exception):
    """A failure as psycopg raises one: its SQLSTATE as `sqlstate`."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__(f"failed ({sqlstate})")
        self.sqlstate = sqlstate


class Statements:
    """The statements the connections run, failing those that make the tables with each of `errors` in turn."""

    def __init__(self, ensuring: str, errors: Sequence[BaseException]) -> None:
        self.ensuring = ensuring
        self.errors = list(errors)
        self.run: list[str] = []

    def record(self, statement: str) -> None:
        self.run.append(statement)
        if statement == self.ensuring and self.errors:
            raise self.errors.pop(0)

    @property
    def made(self) -> int:
        return self.run.count(self.ensuring)


class PostgresConnection:
    def __init__(self, statements: Statements) -> None:
        self.statements = statements
        self.info = type("Info", (), {"backend_pid": 7})()

    def execute(self, statement: str) -> None:
        self.statements.record(statement)


class PostgresPool:
    def __init__(self, statements: Statements) -> None:
        self.statements = statements
        self.out = 0

    def open(self) -> None:
        pass

    def getconn(self) -> PostgresConnection:
        self.out += 1
        return PostgresConnection(self.statements)

    def putconn(self, _: Any) -> None:
        self.out -= 1


class OracleCursor:
    def __init__(self, statements: Statements) -> None:
        self.statements = statements

    def execute(self, statement: str) -> None:
        self.statements.record(statement)

    def fetchone(self) -> tuple[int]:
        return (7,)

    def close(self) -> None:
        pass


class OracleConnection:
    def __init__(self, statements: Statements) -> None:
        self.statements = statements

    def cursor(self) -> OracleCursor:
        return OracleCursor(self.statements)


class OraclePool:
    def __init__(self, statements: Statements) -> None:
        self.statements = statements
        self.out = 0

    def acquire(self) -> OracleConnection:
        self.out += 1
        return OracleConnection(self.statements)

    def release(self, _: Any) -> None:
        self.out -= 1


def postgres(errors: Sequence[BaseException] = ()) -> tuple[PostgresConnectionProvider, Statements]:
    provider = PostgresConnectionProvider("pg", "localhost", 5432, "drivine")
    statements = Statements(provider.graph.ensuring, errors)
    provider._pool = PostgresPool(statements)
    return provider, statements


def oracle(errors: Sequence[BaseException] = ()) -> tuple[OracleConnectionProvider, Statements]:
    provider = OracleConnectionProvider("ora", "localhost:1521/FREEPDB1")
    statements = Statements(provider.graph.ensuring, errors)
    provider._pool = OraclePool(statements)
    return provider, statements


@pytest.fixture
def no_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runs a block again at once, rather than after the pause the store would have."""
    monkeypatch.setattr(contention, "CONTENDED_PAUSE_SECONDS", 0)


@pytest.mark.usefixtures("no_pause")
@pytest.mark.parametrize(
    ("make", "failure"),
    [(oracle, lambda: RuntimeError(DEADLOCK)), (postgres, lambda: PostgresError("40P01"))],
)
def test_runs_the_block_again_when_a_deadlock_turned_it_away(make: Any, failure: Any):
    provider, statements = make(errors=[failure(), failure()])
    provider.connect()
    assert statements.made == 3
    assert provider._pool.out == 1


@pytest.mark.usefixtures("no_pause")
@pytest.mark.parametrize(
    ("make", "failure"),
    [(oracle, lambda: RuntimeError(BUSY)), (postgres, lambda: PostgresError("55P03"))],
)
def test_runs_the_block_once_when_its_lock_stayed_busy_and_the_connection_goes_back(make: Any, failure: Any):
    provider, statements = make(errors=[failure()])
    with pytest.raises(type(failure())):
        provider.connect()
    assert statements.made == 1
    assert provider._pool.out == 0


@pytest.mark.parametrize(
    ("make", "failure"),
    [
        (oracle, lambda: RuntimeError("ORA-06512: at line 22\nORA-01031: insufficient privileges")),
        (postgres, lambda: PostgresError("25006")),
    ],
)
def test_a_store_that_will_not_make_the_tables_hands_out_the_connection_and_is_not_asked_again(
    make: Any, failure: Any, caplog: pytest.LogCaptureFixture
):
    provider, statements = make(errors=[failure(), failure()])
    with caplog.at_level(logging.WARNING, logger="drivine.connection"):
        provider.connect()
        provider.connect()
    assert statements.made == 1
    assert provider._pool.out == 2
    assert len([it for it in caplog.records if "not tried again" in it.getMessage()]) == 1


@pytest.mark.usefixtures("no_pause")
@pytest.mark.parametrize(
    ("make", "failure"),
    [(oracle, lambda: RuntimeError(DEADLOCK)), (postgres, lambda: PostgresError("40P01"))],
)
def test_a_schema_statement_does_not_run_again_the_first_use_block_its_first_connection_gave_up_on(
    make: Any, failure: Any
):
    provider, statements = make(errors=[failure() for _ in range(CONTENDED_ATTEMPTS * CONTENDED_ATTEMPTS)])
    with pytest.raises(type(failure())):
        SchemaExecutor(provider).run("CREATE INDEX x ON y (z)")
    assert statements.made == CONTENDED_ATTEMPTS
