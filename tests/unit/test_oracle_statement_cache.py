"""
On Oracle a statement compiled from Cypher is left out of python-oracledb's statement cache, and a SQL one is kept:
a kept compiled statement holds megabytes of Oracle's memory (liberation-data/drivine4j#313, #218), and Drivine's own
SQL is cheap to keep. drivine4j's JdbcConnectionTest of it. And a JSON column the driver parses is read in the store
layout for Drivine's own statements alone.
"""

from typing import Any

import pytest

from drivine import QuerySpecification, cypher_statement
from drivine.connection.oracle.oracle_connection import OracleConnection
from drivine.cursor.cursor_specification import CursorSpecification
from drivine.sync.connection.oracle.oracle_connection import OracleConnection as SyncOracleConnection

pytestmark = pytest.mark.usefixtures("no_labelled_indexes")


class Cursor:
    def __init__(self, session: "Session") -> None:
        self.session = session
        self.description: Any = None
        self.arraysize = 100

    def prepare(self, statement: str, tag: str | None = None, cache_statement: bool = True) -> None:
        self.session.prepared.append((statement, cache_statement))

    async def execute(self, statement: str, parameters: Any = None, **_: Any) -> None:
        self.session.run.append(statement)

    def close(self) -> None:
        pass


class SyncCursor(Cursor):
    def execute(self, statement: str, parameters: Any = None, **_: Any) -> None:  # type: ignore[override]
        self.session.run.append(statement)


class Session:
    """A session whose statements give no rows, and which records what is prepared and run on it."""

    def __init__(self, sync: bool = False) -> None:
        self.sync = sync
        self.prepared: list[tuple[str, bool]] = []
        self.run: list[str] = []
        self.autocommit = True

    def cursor(self) -> Cursor:
        return SyncCursor(self) if self.sync else Cursor(self)


READ = "MATCH (n:Claim) RETURN n.id"
SQL = "SELECT 1 FROM dual"


async def test_a_statement_compiled_from_cypher_is_left_out_of_the_cache_and_a_sql_one_is_kept():
    session = Session()
    connection = OracleConnection(session, None, "s", "cache_unit", "db|me")
    await connection.query(QuerySpecification[Any](cypher_statement(READ)))
    await connection.query(QuerySpecification[Any](SQL))
    # And through a cursor.
    await connection.open_cursor(CursorSpecification[Any](cypher_statement(READ)))
    await connection.open_cursor(CursorSpecification[Any](SQL))
    compiled, sql, opened, opened_sql = session.run
    assert sql == SQL and opened_sql == SQL and compiled == opened
    # Prepared as the text it is run as, so it is not prepared again.
    assert session.prepared == [(compiled, False), (opened, False)]


def test_the_sync_api_leaves_a_compiled_statement_out_of_the_cache_too():
    session = Session(sync=True)
    connection = SyncOracleConnection(session, None, "s", "cache_unit", "db|me")
    connection.query(QuerySpecification[Any](cypher_statement(READ)))
    connection.query(QuerySpecification[Any](SQL))
    connection.open_cursor(CursorSpecification[Any](cypher_statement(READ)))
    connection.open_cursor(CursorSpecification[Any](SQL))
    compiled, sql, opened, opened_sql = session.run
    assert sql == SQL and opened_sql == SQL
    assert session.prepared == [(compiled, False), (opened, False)]


TAG = {"$t": "date", "v": "2024-01-01", "s": "10000020240101"}


class RowCursor(Cursor):
    """A cursor whose statement gives one row of one JSON column, parsed as python-oracledb hands Oracle's JSON type."""

    def __init__(self, session: "Session") -> None:
        super().__init__(session)
        self.description = [("RESULT",)]

    async def fetchall(self) -> list[Any]:
        return [[{"d": dict(TAG)}]]

    async def fetchmany(self, count: int) -> list[Any]:
        return [[{"d": dict(TAG)}]]


class RowSession(Session):
    def cursor(self) -> Cursor:
        return RowCursor(self)


async def test_a_json_column_is_read_in_the_store_layout_for_drivines_own_sql_and_not_for_a_users():
    connection = OracleConnection(RowSession(), None, "s", "cache_unit", "db|me")
    own = QuerySpecification[Any](SQL)
    own.store_layout = True
    assert await connection.query(own) == [{"d": "2024-01-01"}]
    assert await connection.query(QuerySpecification[Any](SQL)) == [{"d": TAG}]
    # And through a cursor.
    opened = CursorSpecification[Any](SQL)
    opened.store_layout = True
    assert (await (await connection.open_cursor(opened)).read(10))[0] == [{"d": "2024-01-01"}]
    assert (await (await connection.open_cursor(CursorSpecification[Any](SQL))).read(10))[0] == [{"d": TAG}]
