"""
Each session Oracle's pool opens is set, before any other statement, to compare text by its code points as the shared
module states (#566), and to make no adaptive plan (#146). A session that can't be set is dropped from the pool, its
error raised, rather than given out unset. The application can ask for its plans to be left alone, not its text order.
OracleAdaptivePlans and OracleSessionText in the integration tests ask Oracle.
"""

from typing import Any

import oracledb
import pytest

from drivine.backend.store_statements import store_statements
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


SESSION = store_statements("oracle", "drivine").session or []
"""What the shared module has each connection run as it opens: the session's text order, from compiler 0.51.0."""


def test_the_module_states_the_session_that_compares_text_by_its_code_points():
    assert SESSION == ["ALTER SESSION SET NLS_COMP = BINARY NLS_SORT = BINARY"]


async def test_a_new_session_runs_the_modules_session_statement_before_any_other(pools: list[Pool]):
    provider = OracleConnectionProvider("ora", "localhost:1521/FREEPDB1")
    provider._pool_of()
    session = Session()
    await pools[0].options["session_callback"](session, None)
    assert session.statements == [*SESSION, NO_ADAPTIVE_PLANS]
    assert provider.session_statements() == session.statements


async def test_a_session_the_pool_opens_is_set_to_make_no_adaptive_plan(pools: list[Pool]):
    OracleConnectionProvider("ora", "localhost:1521/FREEPDB1")._pool_of()
    session = Session()
    await pools[0].options["session_callback"](session, None)
    assert session.statements[-1] == NO_ADAPTIVE_PLANS
    assert pools[0].dropped == []


async def test_a_session_that_cannot_be_set_is_dropped_and_its_error_raised(pools: list[Pool]):
    OracleConnectionProvider("ora", "localhost:1521/FREEPDB1")._pool_of()
    refused = RuntimeError("ORA-02248: invalid option for ALTER SESSION")
    session = Session(fails=refused)
    with pytest.raises(RuntimeError) as raised:
        await pools[0].options["session_callback"](session, None)
    assert raised.value is refused
    assert pools[0].dropped == [session]


async def test_the_plans_are_left_as_oracle_makes_them_when_the_application_asks_and_the_text_order_set(
    pools: list[Pool],
):
    OracleConnectionProvider("ora", "localhost:1521/FREEPDB1", sets_no_adaptive_plans=False)._pool_of()
    SyncOracleConnectionProvider("ora", "localhost:1521/FREEPDB1", sets_no_adaptive_plans=False)._pool_of()
    session, sync_session = Session(), SyncSession()
    await pools[0].options["session_callback"](session, None)
    pools[1].options["session_callback"](sync_session, None)
    assert session.statements == sync_session.statements == SESSION


def test_a_session_the_sync_pool_opens_is_set_and_one_that_cannot_be_is_dropped(pools: list[Pool]):
    SyncOracleConnectionProvider("ora", "localhost:1521/FREEPDB1")._pool_of()
    callback = pools[0].options["session_callback"]
    session = SyncSession()
    callback(session, None)
    assert session.statements == [*SESSION, NO_ADAPTIVE_PLANS]
    refused = SyncSession(fails=RuntimeError("ORA-03113"))
    with pytest.raises(RuntimeError, match="ORA-03113"):
        callback(refused, None)
    assert pools[0].dropped == [refused]
