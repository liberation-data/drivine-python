"""
Drivine needs Oracle 23.9 or later, whose `UUID()` compiled Cypher's `randomUUID()` is (#252, as
liberation-data/drivine4j#401). The provider reads the version python-oracledb found on its first connection, once for
the pool: on an older Oracle plain SQL runs, and Cypher is refused with the version found. A version that can't be read
refuses nothing. OracleConnection in the integration tests reads the test image's version.
"""

from typing import Any

import pytest

from drivine import DrivineError, QuerySpecification
from drivine.connection.oracle.oracle_connection_provider import OracleConnectionProvider, oracle_refusal
from drivine.query.statement import cypher_statement
from drivine.sync.connection.oracle.oracle_connection_provider import (
    OracleConnectionProvider as SyncOracleConnectionProvider,
)

REFUSED = (
    "Drivine needs Oracle 23.9 or later (it uses UUID()); this database is 23.8.0.25.4. "
    "Upgrade, or use an Oracle Free image 23.9 or newer. Plain SQL over this provider still runs; Cypher does not."
)


def test_an_oracle_older_than_23_9_is_refused_with_the_version_it_is_and_why_and_23_9_or_later_is_not():
    assert oracle_refusal("23.8.0.25.4") == REFUSED
    assert "this database is 19.3.0.0.0." in (oracle_refusal("19.3.0.0.0") or "")
    assert oracle_refusal("23.9.0.25.7") is None
    # As python-oracledb gives Oracle Free 23.26.3.
    assert oracle_refusal("23.26.3.0.0") is None
    assert oracle_refusal("26.1.0.0.0") is None
    # A version that can't be read refuses nothing.
    assert oracle_refusal(None) is None
    assert oracle_refusal("") is None
    assert oracle_refusal("unknown") is None


class Session:
    """A session of `version`, counting how often it is read; each statement gives no rows."""

    def __init__(self, version: Any) -> None:
        self._version = version
        self.reads = 0
        self.statements: list[str] = []
        self.autocommit = True

    @property
    def version(self) -> str:
        self.reads += 1
        if isinstance(self._version, Exception):
            raise self._version
        return self._version

    def cursor(self) -> "Cursor":
        return Cursor(self)


class Cursor:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.description = None

    def prepare(self, statement: str, cache_statement: bool = True) -> None:
        pass

    async def execute(self, statement: str, parameters: Any = None, **_: Any) -> None:
        self.session.statements.append(statement)

    async def fetchone(self) -> tuple[int]:
        return (7,)

    def close(self) -> None:
        pass


class SyncSession(Session):
    def cursor(self) -> "SyncCursor":  # type: ignore[override]
        return SyncCursor(self)


class SyncCursor(Cursor):
    def execute(self, statement: str, parameters: Any = None, **_: Any) -> None:  # type: ignore[override]
        self.session.statements.append(statement)

    def fetchone(self) -> tuple[int]:  # type: ignore[override]
        return (7,)


class Pool:
    """Hands out `sessions` in turn, the last again and again."""

    def __init__(self, *sessions: Session) -> None:
        self.sessions = list(sessions)

    async def acquire(self) -> Session:
        return self.sessions.pop(0) if len(self.sessions) > 1 else self.sessions[0]

    async def release(self, _: Any) -> None:
        pass


class SyncPool(Pool):
    def acquire(self) -> Session:  # type: ignore[override]
        return self.sessions.pop(0) if len(self.sessions) > 1 else self.sessions[0]

    def release(self, _: Any) -> None:  # type: ignore[override]
        pass


def provider(*sessions: Session) -> OracleConnectionProvider:
    it = OracleConnectionProvider("ora", "localhost:1521/FREEPDB1")
    it._pool = Pool(*sessions)
    return it


def cypher(text: str) -> QuerySpecification[Any]:
    return QuerySpecification[Any](cypher_statement(text))


async def test_on_an_oracle_older_than_23_9_a_connection_runs_sql_and_cypher_is_refused_the_version_read_once():
    # The first connection makes the graph's tables and is then handed out; the second is only handed out.
    first, second = Session("23.8.0.25.4"), Session("23.8.0.25.4")
    it = provider(first, second)

    connection = await it.connect()
    assert await connection.query(QuerySpecification[Any]("SELECT 1 FROM dual")) == []
    with pytest.raises(DrivineError) as refused:
        await connection.query(cypher("RETURN randomUUID()"))
    assert str(refused.value) == REFUSED
    with pytest.raises(DrivineError, match="Drivine needs Oracle 23.9 or later"):
        await (await it.connect()).query(cypher("RETURN 1"))

    assert (first.reads, second.reads) == (1, 0)


@pytest.mark.parametrize("version", ["23.9.0.25.7", "23.26.3.0.0", RuntimeError("closed")])
async def test_cypher_runs_on_oracle_23_9_or_later_and_where_the_version_cannot_be_read(version: Any):
    session = Session(version)
    connection = await provider(session).connect()
    before = len(session.statements)
    await connection.query(cypher("RETURN 1"))
    assert len(session.statements) == before + 1


def test_the_sync_provider_refuses_cypher_on_an_oracle_older_than_23_9_and_runs_sql():
    it = SyncOracleConnectionProvider("ora", "localhost:1521/FREEPDB1")
    it._pool = SyncPool(SyncSession("23.8.0.25.4"))
    connection = it.connect()
    assert connection.query(QuerySpecification[Any]("SELECT 1 FROM dual")) == []
    with pytest.raises(DrivineError, match="this database is 23.8.0.25.4"):
        connection.query(cypher("RETURN 1"))
