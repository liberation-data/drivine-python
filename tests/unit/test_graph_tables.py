"""The graph's tables a SQL store's provider makes on its first connection: once per pool, again after a failure."""

import asyncio

import pytest

from drivine.connection.graph_tables import GraphTables


class Recording:
    """Records the statements it runs, and fails the first `failures` of them."""

    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.statements: list[str] = []

    async def run(self, statement: str) -> None:
        self.statements.append(statement)
        # Gives way while it runs, as a statement sent to a database does, so another connection may ask meanwhile.
        await asyncio.sleep(0)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("the tables could not be made")


async def test_makes_the_tables_once_for_the_pool():
    tables, recording = GraphTables("MAKE TABLES"), Recording()
    await tables.ensure(recording.run)
    await tables.ensure(recording.run)
    assert recording.statements == ["MAKE TABLES"]


async def test_makes_the_tables_once_for_connections_that_ask_at_once():
    tables, recording = GraphTables("MAKE TABLES"), Recording()
    await asyncio.gather(*(tables.ensure(recording.run) for _ in range(5)))
    assert recording.statements == ["MAKE TABLES"]


async def test_makes_the_tables_again_at_the_next_connection_when_making_them_failed():
    tables, recording = GraphTables("MAKE TABLES"), Recording(failures=1)
    with pytest.raises(RuntimeError, match="could not be made"):
        await tables.ensure(recording.run)
    await tables.ensure(recording.run)
    await tables.ensure(recording.run)
    assert recording.statements == ["MAKE TABLES", "MAKE TABLES"]


async def test_makes_the_tables_again_once_the_pool_is_forgotten():
    tables, recording = GraphTables("MAKE TABLES"), Recording()
    await tables.ensure(recording.run)
    tables.forget()
    await tables.ensure(recording.run)
    assert recording.statements == ["MAKE TABLES", "MAKE TABLES"]
