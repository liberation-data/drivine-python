"""
One `sqlite3` connection as Drivine opens it, and the calls on it that block: the async and sync connections both run
them, the async one on a thread of its own (see concurrency.blocking).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from datetime import time as clock_time
from decimal import Decimal
from enum import Enum
from types import ModuleType
from typing import Any, NoReturn, cast
from uuid import UUID

from drivine.backend.sql.sql_json import storable, to_json
from drivine.backend.sqlite import sqlite_functions
from drivine.backend.sqlite.sqlite_contention import ERRORS, locked
from drivine.backend.sqlite.sqlite_graph import JSON_ROWS, SqliteGraph
from drivine.drivine_error import DrivineError

logger = logging.getLogger("drivine.connection")

MINIMUM_VERSION = (3, 45, 0)
"""The oldest SQLite Drivine's statements run on: drivine4j's, for its JSON functions and aggregate order."""

DEFAULT_BUSY_TIMEOUT = 30_000
"""How long a connection waits for a locked file, in ms: long enough for any one save to finish."""

LOCK_WARNING_SECONDS = 5.0
"""How long a transaction may hold the file's write lock before its end is logged as a warning."""

_FUNCTION_FAILED = "user-defined function raised exception"

_POSITIONAL = re.compile(r"\$(\d+)(?![0-9])")


def sqlite_module() -> ModuleType:
    """
    The `sqlite3` module whose SQLite is 3.45 or later: Python's own, or `pysqlite3` where Python's is older and it is
    installed (`pip install pysqlite3-binary`).
    """
    if sqlite3.sqlite_version_info >= MINIMUM_VERSION:
        return sqlite3
    try:
        import importlib

        dbapi2: Any = importlib.import_module("pysqlite3.dbapi2")
    except ImportError:
        dbapi2 = None
    if dbapi2 is not None and dbapi2.sqlite_version_info >= MINIMUM_VERSION:
        return cast(ModuleType, dbapi2)
    raise DrivineError(
        "Drivine keeps a graph in SQLite 3.45 or later, and Python's sqlite3 module is SQLite "
        f"{sqlite3.sqlite_version}. "
        "Install one that is later: pip install pysqlite3-binary, or a Python built with a later SQLite."
    )


class SqliteSession:
    """
    A connection to the SQLite file at `path`, opened as Drivine's statements want it, with the functions they call
    registered on it (see sqlite_functions):

    - The journal is a write-ahead log, so readers do not wait for the writer, nor the writer for them.
    - Foreign keys are enforced, so a node's relationships go with it.
    - A connection that finds the file locked waits for it, up to `busy_timeout` ms, before it gives up.
    - A transaction takes the write lock as it begins (`BEGIN IMMEDIATE`), so one that reads and then writes is not
      turned away for what another wrote in between: two such transactions wait their turn.
    - The log is synced at a checkpoint and not at each commit, as is safe with a write-ahead log: a commit may be lost
      to a power cut, and the file is never left corrupt.

    Outside a transaction each statement commits as it runs. It is used by one caller at a time. The async API runs
    each call on `thread`, the connection's own: so a connection that waits for the file's lock keeps only its own
    thread waiting, never one the connection holding the lock needs to finish, and a call made after one whose caller
    was cancelled runs once that one has ended, never beside it.
    """

    def __init__(self, path: str, busy_timeout: int = DEFAULT_BUSY_TIMEOUT) -> None:
        module = sqlite_module()
        try:
            self.connection: sqlite3.Connection = module.connect(
                path, timeout=busy_timeout / 1000, isolation_level=None, check_same_thread=False
            )
        except module.Error as error:
            # SQLite's message, as for a folder that is not there, does not name the file.
            raise DrivineError(f"Could not open the SQLite file '{path}': {error}.") from error
        self.thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="drivine-sqlite")
        self.failures = sqlite_functions.Failures()
        try:
            deadline = time.monotonic() + busy_timeout / 1000
            for pragma in ("journal_mode = WAL", "foreign_keys = ON", "synchronous = NORMAL"):
                while True:
                    try:
                        self.connection.execute(f"PRAGMA {pragma}").close()
                        break
                    except ERRORS as error:
                        # A connection opened as another closes, or recovers the log, is turned away at once, without
                        # SQLite's wait: it tries again, as long as it waits for a locked file.
                        if not locked(error) or time.monotonic() >= deadline:
                            raise
                        time.sleep(0.005)
            self.connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout)}").close()
            sqlite_functions.register(self.connection, self.failures)
        except BaseException:
            self.connection.close()
            raise
        self._started: float | None = None

    @property
    def in_transaction(self) -> bool:
        return self._started is not None

    def query(self, statement: str, parameters: Mapping[str, Any] | Sequence[Any]) -> list[dict[str, Any]]:
        """The rows the statement gives, each a dict of its columns: see `rows`."""
        cursor = self.execute(statement, parameters)
        try:
            return rows(statement, cursor, cursor.fetchall())
        except ERRORS as error:
            # A function may fail at a later row, as it reaches it.
            self._raise_told(error)
        finally:
            cursor.close()

    def execute(self, statement: str, parameters: Mapping[str, Any] | Sequence[Any]) -> sqlite3.Cursor:
        """
        Runs the statement with its values bound (see `bindable`), and gives its cursor. A failure inside a function
        Drivine registered is raised as itself, not as the "user-defined function raised exception" SQLite says of it.
        """
        sql, values = bindable(statement, parameters)
        try:
            return self.connection.execute(sql, values)
        except ERRORS as error:
            self._raise_told(error)
        except BaseException:
            self.failures.take()
            raise

    def fetch(self, statement: str, cursor: sqlite3.Cursor, count: int) -> list[dict[str, Any]]:
        """The next `count` rows of the cursor at most."""
        try:
            return rows(statement, cursor, cursor.fetchmany(count))
        except ERRORS as error:
            self._raise_told(error)

    def begin(self) -> None:
        """Opens a transaction, the file's write lock taken."""
        self.connection.execute("BEGIN IMMEDIATE").close()
        self._started = time.monotonic()

    def commit(self) -> None:
        """
        Commits the open transaction. One SQLite refuses to commit, as it does one breaking a deferred foreign key, is
        left open, for the caller to roll back.
        """
        self.connection.execute("COMMIT").close()
        self._ended()

    def rollback(self) -> None:
        """
        Rolls the open transaction back. A `ROLLBACK` refused because SQLite has already rolled the transaction back, as
        it does on a full disk or an interrupted statement, has nothing left to undo.
        """
        try:
            self.connection.execute("ROLLBACK").close()
        except ERRORS:
            if self.connection.in_transaction:
                raise
        finally:
            if not self.connection.in_transaction:
                self._ended()

    def ensure(self, graph: SqliteGraph) -> None:
        """
        Makes the graph's tables where they are not all there, each statement on its own: a user who may only read the
        file is refused a `CREATE … IF NOT EXISTS` even over a table that is there, so the store is looked for first.
        """
        found = self.connection.execute(graph.made).fetchone()
        if found is not None and found[0] == len(graph.ddl):
            return
        for it in graph.ddl:
            self.connection.execute(it).close()

    def close(self) -> None:
        self.connection.close()
        self.thread.shutdown(wait=False)

    def _ended(self) -> None:
        """
        The transaction is over: one that held the write lock past LOCK_WARNING_SECONDS is logged, as every other
        writer to the file waited for it.
        """
        started, self._started = self._started, None
        if started is None:
            return
        held = time.monotonic() - started
        if held > LOCK_WARNING_SECONDS:
            logger.warning(
                "A SQLite transaction held the file's write lock for %d ms: no other writer can write while one is "
                "held.",
                held * 1000,
            )

    def _raise_told(self, error: Exception) -> NoReturn:
        """
        Raises the failure a function raised, caused by SQLite's error, where SQLite says only that one did; the error
        itself otherwise.
        """
        kept = self.failures.take()
        if kept is not None and _FUNCTION_FAILED in str(error):
            raise kept from error
        raise error


def bindable(statement: str, parameters: Mapping[str, Any] | Sequence[Any]) -> tuple[str, Any]:
    """
    The statement and its values as `sqlite3` binds them. SQLite reads `$name` itself, so a statement's named values
    are bound by name; each positional `$1` becomes SQLite's `?1`. A map or a list is bound as its JSON text, as
    Drivine's statements take one; a date as ISO 8601 text, an Enum as its value, and so on, as a save writes them (see
    storable).
    """
    if isinstance(parameters, Mapping):
        return statement, {str(key): _value(value) for key, value in parameters.items()}
    if not parameters:
        return statement, ()
    return _POSITIONAL.sub(lambda it: f"?{it.group(1)}", statement), tuple(_value(it) for it in parameters)


def _value(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple, set, frozenset)):
        return to_json(value)
    if isinstance(value, (datetime, date, clock_time, Enum, Decimal)):
        return storable(value)
    if isinstance(value, UUID):
        return str(value)
    return value


def rows(statement: str, cursor: sqlite3.Cursor, fetched: list[Any]) -> list[dict[str, Any]]:
    """
    The rows as dicts of their columns. Where the statement opens with JSON_ROWS, each text value is JSON, which is
    read: SQLite has no JSON type for a column to say so.
    """
    if cursor.description is None:
        return []
    names = [it[0] for it in cursor.description]
    documents = statement.lstrip().startswith(JSON_ROWS)
    return [
        {
            name: (json.loads(value) if documents and isinstance(value, str) else value)
            for name, value in zip(names, row, strict=True)
        }
        for row in fetched
    ]
