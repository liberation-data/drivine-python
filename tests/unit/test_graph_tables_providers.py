"""
The graph's tables, as a SQL store's provider makes them on a connection: a connection whose making failed goes back to
the pool, the next makes them again, and a pool made after `end()` makes them again. A block a deadlock turned away is
run again on the same connection (#287, liberation-data/drivine#454).
"""

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

import pytest

from drivine.connection.oracle.oracle_connection_provider import OracleConnectionProvider
from drivine.connection.postgres.postgres_connection_provider import PostgresConnectionProvider
from drivine.manager import contention
from drivine.manager.contention import CONTENDED_ATTEMPTS
from drivine.schema.schema_executor import SchemaExecutor

DEADLOCK = "ORA-04020: deadlock detected while trying to lock object DRIVINE.DRIVINE_NODE"
ROW_DEADLOCK = "ORA-06512: at line 22\nORA-00060: deadlock detected while waiting for resource"
BUSY = "ORA-06512: at line 22\nORA-00054: resource busy and acquire with NOWAIT specified or timeout expired"


class PostgresError(Exception):
    """A failure as psycopg raises one: its SQLSTATE as `sqlstate`."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__(f"failed ({sqlstate})")
        self.sqlstate = sqlstate


class Statements:
    """
    The statements the connections run, failing those that make the tables with each of `errors` in turn, then the
    first `failures` of the rest.
    """

    def __init__(self, ensuring: str, failures: int = 0, errors: Sequence[BaseException] = ()) -> None:
        self.ensuring = ensuring
        self.failures = failures
        self.errors = list(errors)
        self.run: list[str] = []
        self.made_by: list[int] = []
        """The connection each run of the block was on, by its number."""

    def record(self, statement: str, connection: int = 0) -> None:
        self.run.append(statement)
        if statement != self.ensuring:
            return
        self.made_by.append(connection)
        if self.errors:
            raise self.errors.pop(0)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("the tables could not be made")

    @property
    def made(self) -> int:
        return self.run.count(self.ensuring)


class PostgresConnection:
    def __init__(self, statements: Statements) -> None:
        self.statements = statements
        self.info = type("Info", (), {"backend_pid": 7})()

    async def execute(self, statement: str) -> None:
        self.statements.record(statement)


class PostgresPool:
    def __init__(self, statements: Statements) -> None:
        self.statements = statements
        self.out = 0

    async def open(self) -> None:
        pass

    async def getconn(self) -> PostgresConnection:
        self.out += 1
        return PostgresConnection(self.statements)

    async def putconn(self, _: Any) -> None:
        self.out -= 1

    async def close(self) -> None:
        pass


class OracleCursor:
    def __init__(self, statements: Statements, connection: int) -> None:
        self.statements = statements
        self.connection = connection

    async def execute(self, statement: str) -> None:
        # Gives way while it runs, as a statement sent to a database does, so another connection may ask meanwhile.
        await asyncio.sleep(0)
        self.statements.record(statement, self.connection)

    async def fetchone(self) -> tuple[int]:
        return (7,)

    def close(self) -> None:
        pass


class OracleConnection:
    def __init__(self, statements: Statements, number: int) -> None:
        self.statements = statements
        self.number = number

    def cursor(self) -> OracleCursor:
        return OracleCursor(self.statements, self.number)


class OraclePool:
    def __init__(self, statements: Statements) -> None:
        self.statements = statements
        self.out = 0
        self.acquired = 0

    async def acquire(self) -> OracleConnection:
        self.out += 1
        self.acquired += 1
        return OracleConnection(self.statements, self.acquired)

    async def release(self, _: Any) -> None:
        self.out -= 1

    async def close(self, force: bool = False) -> None:
        pass


def postgres(failures: int = 0, errors: Sequence[BaseException] = ()) -> tuple[PostgresConnectionProvider, Statements]:
    provider = PostgresConnectionProvider("pg", "localhost", 5432, "drivine")
    statements = Statements(provider.graph.ensuring, failures, errors)
    provider._pool = PostgresPool(statements)
    return provider, statements


def oracle(failures: int = 0, errors: Sequence[BaseException] = ()) -> tuple[OracleConnectionProvider, Statements]:
    provider = OracleConnectionProvider("ora", "localhost:1521/FREEPDB1")
    statements = Statements(provider.graph.ensuring, failures, errors)
    provider._pool = OraclePool(statements)
    return provider, statements


@pytest.fixture
def no_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runs a block again at once, rather than after the pause the store would have."""
    monkeypatch.setattr(contention, "CONTENDED_PAUSE_SECONDS", 0)


@pytest.mark.parametrize("make", [postgres, oracle])
async def test_a_connection_whose_tables_could_not_be_made_goes_back_and_the_next_makes_them(make: Any):
    provider, statements = make(failures=1)
    pool = provider._pool
    with pytest.raises(RuntimeError, match="could not be made"):
        await provider.connect()
    assert pool.out == 0
    await provider.connect()
    await provider.connect()
    assert statements.made == 2
    assert pool.out == 2


@pytest.mark.parametrize("make", [postgres, oracle])
async def test_a_pool_made_after_end_makes_the_tables_again(make: Any):
    provider, statements = make()
    pool = provider._pool
    await provider.connect()
    await provider.end()
    provider._pool = pool
    await provider.connect()
    assert statements.made == 2


@pytest.mark.usefixtures("no_pause")
async def test_oracle_runs_the_block_again_when_a_deadlock_turned_it_away():
    provider, statements = oracle(errors=[RuntimeError(DEADLOCK), RuntimeError(ROW_DEADLOCK)])
    await provider.connect()
    assert statements.made == 3
    assert statements.made_by == [1, 1, 1]


@pytest.mark.usefixtures("no_pause")
@pytest.mark.parametrize(
    ("failure", "code"),
    [
        # A lock still busy has had its DDL_LOCK_TIMEOUT inside the block: waiting it again would hold the connection.
        (BUSY, "ORA-00054"),
        ("ORA-00942: table or view does not exist", "ORA-00942"),
    ],
)
async def test_oracle_runs_the_block_once_when_it_failed_for_another_reason_a_busy_lock_too(failure: str, code: str):
    provider, statements = oracle(errors=[RuntimeError(failure)])
    pool = provider._pool
    with pytest.raises(RuntimeError, match=code):
        await provider.connect()
    assert statements.made == 1
    assert pool.out == 0


@pytest.mark.usefixtures("no_pause")
async def test_postgres_runs_the_block_again_when_a_deadlock_turned_it_away():
    provider, statements = postgres(errors=[PostgresError("40P01"), PostgresError("40P01")])
    await provider.connect()
    assert statements.made == 3


@pytest.mark.usefixtures("no_pause")
@pytest.mark.parametrize("sqlstate", ["XX000", "55P03"])
async def test_postgres_runs_the_block_once_when_it_failed_for_another_reason_a_lock_not_had_in_time_too(
    sqlstate: str,
):
    provider, statements = postgres(errors=[PostgresError(sqlstate)])
    pool = provider._pool
    with pytest.raises(PostgresError) as raised:
        await provider.connect()
    assert raised.value.sqlstate == sqlstate
    assert statements.made == 1
    assert pool.out == 0


@pytest.mark.parametrize(
    ("make", "failure"),
    [
        (oracle, lambda: RuntimeError("ORA-06512: at line 22\nORA-01031: insufficient privileges")),
        (oracle, lambda: RuntimeError("ORA-16000: database or pluggable database open for read-only access")),
        (postgres, lambda: PostgresError("42501")),
        (postgres, lambda: PostgresError("25006")),
    ],
)
async def test_a_store_that_will_not_make_the_tables_hands_out_the_connection_and_is_not_asked_again(
    make: Any, failure: Any, caplog: pytest.LogCaptureFixture
):
    # A user who may not make tables, or a database open read-only, would fail the block on every connection: plain
    # SQL still works, and a statement over tables that are not there says so.
    provider, statements = make(errors=[failure(), failure()])
    pool = provider._pool
    with caplog.at_level(logging.WARNING, logger="drivine.connection"):
        await provider.connect()
        await provider.connect()
    assert statements.made == 1
    assert pool.out == 2
    assert len([it for it in caplog.records if "not tried again" in it.getMessage()]) == 1


@pytest.mark.usefixtures("no_pause")
async def test_hands_out_every_connection_that_waited_while_oracles_block_ran_again_after_a_deadlock():
    provider, statements = oracle(errors=[RuntimeError(DEADLOCK)])
    pool = provider._pool
    handed = await asyncio.gather(*(provider.connect() for _ in range(3)))
    assert len(handed) == 3
    assert statements.made_by == [1, 1]
    assert pool.out == 3


@pytest.mark.usefixtures("no_pause")
@pytest.mark.parametrize(
    ("make", "failure"),
    [(oracle, lambda: RuntimeError(DEADLOCK)), (postgres, lambda: PostgresError("40P01"))],
)
async def test_a_schema_statement_does_not_run_again_the_first_use_block_its_first_connection_gave_up_on(
    make: Any, failure: Any
):
    provider, statements = make(errors=[failure() for _ in range(CONTENDED_ATTEMPTS * CONTENDED_ATTEMPTS)])
    with pytest.raises(type(failure())):
        await SchemaExecutor(provider).run("CREATE INDEX x ON y (z)")
    assert statements.made == CONTENDED_ATTEMPTS


@pytest.mark.usefixtures("no_pause")
async def test_a_schema_statement_does_not_run_again_the_first_use_block_whose_lock_stayed_busy():
    # A schema statement's own ORA-00054 would be run again; the first-use block's has waited its minute already.
    provider, statements = oracle(errors=[RuntimeError(BUSY) for _ in range(CONTENDED_ATTEMPTS)])
    with pytest.raises(RuntimeError, match="ORA-00054"):
        await SchemaExecutor(provider).run("CREATE INDEX x ON y (z)")
    assert statements.made == 1
