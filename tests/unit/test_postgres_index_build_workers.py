"""
An index build on Postgres runs with no parallel workers, and leaves the session's own setting as it was (#435,
liberation-data/drivine4j#626): `PostgresConnection.building_index`, and the statements `SchemaExecutor` sends through
it, in the async API and the sync one, over a psycopg connection faked.
"""

from __future__ import annotations

from typing import Any

import pytest

from drivine import Connection, ConnectionProvider, Cursor, CursorSpecification, DatabaseType, QuerySpecification
from drivine.connection.postgres.postgres_connection import PostgresConnection
from drivine.drivine_error import DrivineError
from drivine.schema.schema_executor import SchemaExecutor
from drivine.sync.connection.postgres.postgres_connection import PostgresConnection as SyncPostgresConnection
from drivine.sync.schema.schema_executor import SchemaExecutor as SyncSchemaExecutor

BUILDS = [
    "CREATE INDEX IF NOT EXISTS g_ix_chunk_a ON g_node ((props ->> 'a')) WHERE labels @> '[\"Chunk\"]'",
    "CREATE UNIQUE INDEX IF NOT EXISTS g_uq_person_email ON g_node ((props ->> 'email'))",
    "create unique index g_uq_x on g_node (nid)",
    "  CREATE INDEX IF NOT EXISTS g_v_chunk_embedding ON g_node USING hnsw ((embedding) vector_cosine_ops)",
]
"""Statements that build an index, as PostgresSchemaGrammar writes them, in any case or spacing."""

OTHERS = [
    "DROP INDEX IF EXISTS g_ix_chunk_a",
    "CREATE EXTENSION IF NOT EXISTS vector",
    "INSERT INTO g_schema (kind, label, properties) VALUES ('RANGE', 'Chunk', '[\"a\"]')",
    "SELECT indexname FROM pg_indexes WHERE tablename = 'g_node'",
    "CREATE INDEXES_TABLE (a int)",
]
"""Statements that build no index."""


class Session:
    """
    What a fake psycopg session was sent, in order: `read`, the setting read; `workers=<n>`, the setting set; `reset`,
    the setting reset; and each other statement as it is. The setting starts at `workers`, the session's own where
    `own`, else the server's; one set to a value in `refused` raises.
    """

    def __init__(self, workers: str = "3", refused: tuple[str, ...] = (), own: bool = True) -> None:
        self.workers = workers
        self.refused = refused
        self.own = own
        self.sent: list[str] = []
        self.released = 0

    def run(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        if "FROM pg_settings WHERE name = 'max_parallel_maintenance_workers'" in sql:
            self.sent.append("read")
            return {"workers": self.workers, "own": self.own}
        if sql == "RESET max_parallel_maintenance_workers":
            self.sent.append("reset")
            return None
        if "set_config('max_parallel_maintenance_workers'" in sql:
            assert ", false)" in sql, "the setting is the session's, not a transaction's"
            [value] = params
            if value in self.refused:
                raise RuntimeError(f"could not set the setting to {value}")
            self.workers = value
            self.sent.append(f"workers={value}")
            return None
        self.sent.append(sql)
        return None


class AsyncCursor:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.row: dict[str, Any] | None = None
        self.description = None

    async def __aenter__(self) -> AsyncCursor:
        return self

    async def __aexit__(self, *_: object) -> None:
        pass

    async def execute(self, sql: str, params: Any = None) -> None:
        self.row = self.session.run(sql, params)

    async def fetchone(self) -> dict[str, Any] | None:
        return self.row


class AsyncSession:
    """A psycopg AsyncConnection, faked over a Session."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def cursor(self, **_: object) -> AsyncCursor:
        return AsyncCursor(self.session)

    async def execute(self, sql: str, params: Any = None) -> None:
        self.session.run(sql, params)


class SyncCursor:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.row: dict[str, Any] | None = None
        self.description = None

    def __enter__(self) -> SyncCursor:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def execute(self, sql: str, params: Any = None) -> None:
        self.row = self.session.run(sql, params)

    def fetchone(self) -> dict[str, Any] | None:
        return self.row


class SyncSession:
    """A psycopg Connection, faked over a Session."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def cursor(self, **_: object) -> SyncCursor:
        return SyncCursor(self.session)

    def execute(self, sql: str, params: Any = None) -> None:
        self.session.run(sql, params)


def async_connection(session: Session) -> PostgresConnection:
    async def release(_: object) -> None:
        session.released += 1

    return PostgresConnection(AsyncSession(session), release, "postgres.1")


def sync_connection(session: Session) -> SyncPostgresConnection:
    def release(_: object) -> None:
        session.released += 1

    return SyncPostgresConnection(SyncSession(session), release, "postgres.1")


class PostgresProvider(ConnectionProvider):
    """Postgres connections, each over the one session faked."""

    def __init__(self, session: Session) -> None:
        self.name = "postgres"
        self.type = DatabaseType.POSTGRES
        self.session = session

    async def connect(self) -> Connection:
        return async_connection(self.session)

    async def end(self) -> None:
        pass


class SyncPostgresProvider:
    def __init__(self, session: Session) -> None:
        self.name = "postgres"
        self.type = DatabaseType.POSTGRES
        self.session = session

    def connect(self) -> SyncPostgresConnection:
        return sync_connection(self.session)


class OtherConnection(Connection):
    """A connection to a store other than Postgres, which records what it runs: it has no `building_index`."""

    def __init__(self, sent: list[str]) -> None:
        self.sent = sent

    async def query(self, spec: QuerySpecification[Any]) -> list[Any]:
        assert spec.statement is not None
        self.sent.append(spec.statement.text)
        return []

    async def open_cursor(self, spec: CursorSpecification[Any]) -> Cursor[Any]:
        raise NotImplementedError

    async def start_transaction(self) -> None:
        raise NotImplementedError

    async def commit_transaction(self) -> None:
        raise NotImplementedError

    async def rollback_transaction(self) -> None:
        raise NotImplementedError

    async def release(self, error: BaseException | None = None) -> None:
        pass


class OtherProvider(ConnectionProvider):
    def __init__(self, type: DatabaseType) -> None:
        self.name = type.value.lower()
        self.type = type
        self.sent: list[str] = []

    async def connect(self) -> Connection:
        return OtherConnection(self.sent)

    async def end(self) -> None:
        pass


async def test_a_build_runs_with_no_parallel_workers_and_the_sessions_own_setting_is_put_back_after():
    session = Session(workers="3")

    async def build() -> str:
        session.sent.append(f"build at workers={session.workers}")
        return "built"

    assert await async_connection(session).building_index(build) == "built"
    assert session.sent == ["read", "workers=0", "build at workers=0", "workers=3"]
    assert session.workers == "3"


async def test_the_setting_is_put_back_when_the_build_fails_and_the_builds_error_is_the_one_raised():
    session = Session(workers="3")

    async def build() -> str:
        session.sent.append("build")
        raise ValueError("the build failed")

    with pytest.raises(ValueError, match="the build failed"):
        await async_connection(session).building_index(build)
    assert session.sent == ["read", "workers=0", "build", "workers=3"]
    assert session.workers == "3"


async def test_a_setting_the_session_didnt_set_itself_is_reset_after_the_build_not_set():
    session = Session(workers="2", own=False)

    async def build() -> str:
        return "built"

    assert await async_connection(session).building_index(build) == "built"
    assert session.sent == ["read", "workers=0", "reset"]


async def test_the_builds_error_is_the_one_raised_when_the_setting_cant_be_put_back_either():
    session = Session(workers="3", refused=("3",))

    async def build() -> str:
        raise ValueError("the build failed")

    with pytest.raises(ValueError, match="the build failed"):
        await async_connection(session).building_index(build)
    assert session.sent == ["read", "workers=0"]


def test_the_sync_api_runs_a_build_with_no_parallel_workers_and_puts_the_setting_back_whether_it_passes_or_fails():
    session = Session(workers="2")

    def build() -> str:
        session.sent.append(f"build at workers={session.workers}")
        return "built"

    assert sync_connection(session).building_index(build) == "built"
    assert session.sent == ["read", "workers=0", "build at workers=0", "workers=2"]

    failing = Session(workers="2")

    def fails() -> str:
        raise ValueError("the build failed")

    with pytest.raises(ValueError, match="the build failed"):
        sync_connection(failing).building_index(fails)
    assert failing.sent == ["read", "workers=0", "workers=2"]

    refused = Session(workers="2", refused=("2",))
    with pytest.raises(ValueError, match="the build failed"):
        sync_connection(refused).building_index(fails)


@pytest.mark.parametrize("statement", BUILDS)
async def test_the_schema_executor_builds_an_index_on_postgres_with_no_parallel_workers(statement: str):
    session = Session(workers="3")
    assert await SchemaExecutor(PostgresProvider(session)).run(statement) == []
    assert session.sent == ["read", "workers=0", statement.strip(), "workers=3"]
    assert session.released == 1


@pytest.mark.parametrize("statement", BUILDS)
def test_the_sync_schema_executor_builds_an_index_on_postgres_with_no_parallel_workers(statement: str):
    session = Session(workers="3")
    assert SyncSchemaExecutor(SyncPostgresProvider(session)).run(statement) == []  # pyright: ignore[reportArgumentType]
    assert session.sent == ["read", "workers=0", statement.strip(), "workers=3"]
    assert session.released == 1


@pytest.mark.parametrize("statement", OTHERS)
async def test_the_schema_executor_runs_any_other_statement_on_postgres_as_it_is(statement: str):
    session = Session(workers="3")
    await SchemaExecutor(PostgresProvider(session)).run(statement)
    assert session.sent == [statement]


@pytest.mark.parametrize("statement", OTHERS)
def test_the_sync_schema_executor_runs_any_other_statement_on_postgres_as_it_is(statement: str):
    session = Session(workers="3")
    SyncSchemaExecutor(SyncPostgresProvider(session)).run(statement)  # pyright: ignore[reportArgumentType]
    assert session.sent == [statement]


async def test_a_failed_build_on_postgres_is_raised_as_a_drivine_error_with_the_setting_put_back():
    session = Session(workers="3")

    class Failing(PostgresProvider):
        async def connect(self) -> Connection:
            connection = async_connection(self.session)

            async def query(spec: QuerySpecification[Any]) -> list[Any]:
                self.session.sent.append("build")
                raise ValueError("could not resize shared memory segment")

            connection.query = query  # pyright: ignore[reportAttributeAccessIssue]
            return connection

    with pytest.raises(DrivineError, match="could not resize shared memory segment"):
        await SchemaExecutor(Failing(session)).run(BUILDS[0])
    assert session.sent == ["read", "workers=0", "build", "workers=3"]
    assert session.released == 1


@pytest.mark.parametrize("type", [DatabaseType.NEO4J, DatabaseType.SQLITE, DatabaseType.ORACLE])
async def test_on_another_store_an_index_build_is_run_as_it_is(type: DatabaseType):
    provider = OtherProvider(type)
    statement = "CREATE INDEX x_ix ON x_node (a)"
    await SchemaExecutor(provider).run(statement)
    assert provider.sent == [statement]
