"""
Each session Oracle's pool opens is set to make no adaptive plan (#146), and a session that can't be set is dropped from
the pool, its error raised, rather than given out unset. The application can ask for its sessions to be left alone.
OracleAdaptivePlans in the integration tests asks Oracle for the plan.
"""

from typing import Any

import oracledb
import pytest

from drivine.connection.oracle.oracle_connection_provider import NO_ADAPTIVE_PLANS, OracleConnectionProvider
from drivine.sync.connection.oracle.oracle_connection_provider import (
    OracleConnectionProvider as SyncOracleConnectionProvider,
)


class Session:
    """A session the pool opened: the statements run in it, failing them all when `fails` is set."""

    def __init__(self, fails: Exception | None = None) -> None:
        self.fails = fails
        self.statements: list[str] = []

    def cursor(self) -> "Session":
        return self

    def _run(self, statement: str) -> None:
        self.statements.append(statement)
        if self.fails is not None:
            raise self.fails

    async def execute(self, statement: str) -> None:
        self._run(statement)

    def close(self) -> None:
        pass


class SyncSession(Session):
    def execute(self, statement: str) -> None:  # type: ignore[override]
        self._run(statement)


class Pool:
    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options
        self.dropped: list[Any] = []

    async def drop(self, connection: Any) -> None:
        self.dropped.append(connection)


class SyncPool(Pool):
    def drop(self, connection: Any) -> None:  # type: ignore[override]
        self.dropped.append(connection)


@pytest.fixture
def pools(monkeypatch: pytest.MonkeyPatch) -> list[Pool]:
    """The pools the providers make, in place of python-oracledb's, holding the options they were made with."""
    made: list[Pool] = []

    def create(kind: type[Pool]) -> Any:
        def make(**options: Any) -> Pool:
            made.append(kind(options))
            return made[-1]

        return make

    monkeypatch.setattr(oracledb, "create_pool_async", create(Pool))
    monkeypatch.setattr(oracledb, "create_pool", create(SyncPool))
    return made


async def test_a_session_the_pool_opens_is_set_to_make_no_adaptive_plan(pools: list[Pool]):
    OracleConnectionProvider("ora", "localhost:1521/FREEPDB1")._pool_of()
    session = Session()
    await pools[0].options["session_callback"](session, None)
    assert session.statements == [NO_ADAPTIVE_PLANS]
    assert pools[0].dropped == []


async def test_a_session_that_cannot_be_set_is_dropped_and_its_error_raised(pools: list[Pool]):
    OracleConnectionProvider("ora", "localhost:1521/FREEPDB1")._pool_of()
    refused = RuntimeError("ORA-02248: invalid option for ALTER SESSION")
    session = Session(fails=refused)
    with pytest.raises(RuntimeError) as raised:
        await pools[0].options["session_callback"](session, None)
    assert raised.value is refused
    assert pools[0].dropped == [session]


def test_the_sessions_are_left_as_oracle_opens_them_when_the_application_asks(pools: list[Pool]):
    OracleConnectionProvider("ora", "localhost:1521/FREEPDB1", sets_no_adaptive_plans=False)._pool_of()
    SyncOracleConnectionProvider("ora", "localhost:1521/FREEPDB1", sets_no_adaptive_plans=False)._pool_of()
    assert [it.options["session_callback"] for it in pools] == [None, None]


def test_a_session_the_sync_pool_opens_is_set_and_one_that_cannot_be_is_dropped(pools: list[Pool]):
    SyncOracleConnectionProvider("ora", "localhost:1521/FREEPDB1")._pool_of()
    callback = pools[0].options["session_callback"]
    session = SyncSession()
    callback(session, None)
    assert session.statements == [NO_ADAPTIVE_PLANS]
    refused = SyncSession(fails=RuntimeError("ORA-03113"))
    with pytest.raises(RuntimeError, match="ORA-03113"):
        callback(refused, None)
    assert pools[0].dropped == [refused]
