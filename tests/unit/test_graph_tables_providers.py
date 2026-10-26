"""
The graph's tables, as a SQL store's provider makes them on a connection: a connection whose making failed goes back to
the pool, the next makes them again, and a pool made after `end()` makes them again.
"""

from typing import Any

import pytest

from drivine.connection.oracle.oracle_connection_provider import OracleConnectionProvider
from drivine.connection.postgres.postgres_connection_provider import PostgresConnectionProvider


class Statements:
    """The statements the connections run, failing the first `failures` that make the tables."""

    def __init__(self, ensuring: str, failures: int = 0) -> None:
        self.ensuring = ensuring
        self.failures = failures
        self.run: list[str] = []

    def record(self, statement: str) -> None:
        self.run.append(statement)
        if statement == self.ensuring and self.failures:
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
    def __init__(self, statements: Statements) -> None:
        self.statements = statements

    async def execute(self, statement: str) -> None:
        self.statements.record(statement)

    async def fetchone(self) -> tuple[int]:
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

    async def acquire(self) -> OracleConnection:
        self.out += 1
        return OracleConnection(self.statements)

    async def release(self, _: Any) -> None:
        self.out -= 1

    async def close(self, force: bool = False) -> None:
        pass


def postgres(failures: int = 0) -> tuple[PostgresConnectionProvider, Statements]:
    provider = PostgresConnectionProvider("pg", "localhost", 5432, "drivine")
    statements = Statements(provider.graph.ensuring, failures)
    provider._pool = PostgresPool(statements)
    return provider, statements


def oracle(failures: int = 0) -> tuple[OracleConnectionProvider, Statements]:
    provider = OracleConnectionProvider("ora", "localhost:1521/FREEPDB1")
    statements = Statements(provider.graph.ensuring, failures)
    provider._pool = OraclePool(statements)
    return provider, statements


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
