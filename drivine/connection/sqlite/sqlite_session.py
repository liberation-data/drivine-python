# apsw's stubs type a bytes value as collections.abc.Buffer, which Python 3.11 lacks: what they give is partly unknown.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
"""
One apsw connection to a SQLite file as Drivine opens it, and the calls on it that block: the async and sync connections
both run them, the async one on a thread of its own (see concurrency.blocking).
"""

from __future__ import annotations

import itertools
import json
import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from datetime import time as clock_time
from decimal import Decimal
from enum import Enum
from typing import Any, NoReturn, TypeVar
from uuid import UUID

import apsw

from drivine.backend.sql.sql_json import storable, to_json
from drivine.backend.sqlite import sqlite_functions
from drivine.backend.sqlite.sqlite_contention import locked
from drivine.backend.sqlite.sqlite_graph import JSON_ROWS, SqliteGraph
from drivine.drivine_error import DrivineError

logger = logging.getLogger("drivine.connection")

T = TypeVar("T")

MINIMUM_VERSION = (3, 53, 0)
"""
The oldest SQLite Drivine keeps a graph in: the first to write a float as text to 17 significant digits by default,
not 15, as compiled Cypher needs to give the floats a graph engine gives, each value being JSON text there. An older one
also refuses a subquery in `json_each` that groups by a column of the row it is in, which a `CALL { … }` that
aggregates may compile to (#376). apsw's wheels bundle a later one.
"""

DEFAULT_BUSY_TIMEOUT = 30_000
"""How long a connection waits for a locked file, in ms: long enough for any one save to finish."""

LOCK_WARNING_SECONDS = 5.0
"""How long a transaction may hold the file's write lock before its end is logged as a warning."""

_POSITIONAL = re.compile(r"\$(\d+)(?![0-9])")

_NO_STATEMENT = re.compile(r"(?:\s+|--[^\n]*|/\*.*?(?:\*/|$)|;)*", re.DOTALL)
"""What may follow a statement and holds none: blanks, comments and semicolons, as `sqlite3` let one end with."""


def sqlite_version() -> tuple[int, ...]:
    """The version of the SQLite apsw runs."""
    return tuple(int(it) for it in apsw.sqlite_lib_version().split("."))


def _check_version() -> None:
    """Refuses a SQLite older than MINIMUM_VERSION, as an apsw built against an older SQLite of the system runs."""
    if sqlite_version() < MINIMUM_VERSION:
        raise DrivineError(
            f"Drivine keeps a graph in SQLite 3.53 or later, and apsw runs SQLite {apsw.sqlite_lib_version()}. "
            'Install apsw\'s wheel, which bundles a later one: pip install "drivine[sqlite]".'
        )


class Rows:
    """
    A statement's cursor, with the names of its columns, as apsw tells them while it runs: they are read as it begins,
    as apsw tells none once its last row is read. A statement that gave no row has none.
    """

    def __init__(self, cursor: apsw.Cursor) -> None:
        self.cursor = cursor
        try:
            self.names: list[str] = [it[0] for it in cursor.get_description()]
        except apsw.ExecutionCompleteError:
            self.names = []

    def close(self) -> None:
        self.cursor.close()


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
        _check_version()
        try:
            # apsw opens no transaction of its own: outside BEGIN … COMMIT each statement commits as it runs.
            self.connection = apsw.Connection(path)
        except apsw.Error as error:
            # SQLite's message, as for a folder that is not there, does not name the file.
            raise DrivineError(f"Could not open the SQLite file '{path}': {error}.") from error
        self.thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="drivine-sqlite")
        try:
            self.connection.set_busy_timeout(int(busy_timeout))
            deadline = time.monotonic() + busy_timeout / 1000
            for pragma in ("journal_mode = WAL", "foreign_keys = ON", "synchronous = NORMAL"):
                while True:
                    try:
                        self.connection.execute(f"PRAGMA {pragma}").close()
                        break
                    except apsw.Error as error:
                        # A connection opened as another closes, or recovers the log, is turned away at once, without
                        # SQLite's wait: it tries again, as long as it waits for a locked file.
                        if not locked(error) or time.monotonic() >= deadline:
                            raise
                        time.sleep(0.005)
            sqlite_functions.register(self.connection)
        except BaseException:
            self.connection.close()
            raise
        self._started: float | None = None

    @property
    def in_transaction(self) -> bool:
        return self._started is not None

    def query(self, statement: str, parameters: Mapping[str, Any] | Sequence[Any]) -> list[dict[str, Any]]:
        """The rows the statement gives, each a dict of its columns: see `rows`."""
        found = self.execute(statement, parameters)
        try:
            return rows(statement, found, found.cursor.fetchall())
        finally:
            found.close()

    def query_writing(self, statement: str, parameters: Mapping[str, Any] | Sequence[Any]) -> list[dict[str, Any]]:
        """`query` of a statement that writes, in a transaction of its own where none is open: see `writing`."""
        return self.writing(lambda: self.query(statement, parameters))

    def execute_writing(self, statement: str, parameters: Mapping[str, Any] | Sequence[Any]) -> Rows:
        """`execute` of a statement that writes, in a transaction of its own where none is open: see `writing`."""
        return self.writing(lambda: self.execute(statement, parameters))

    def writing(self, run: Callable[[], T]) -> T:
        """
        What `run` gives, in a transaction of its own where none is open, which takes the write lock as it begins: a
        statement that reads, then writes within it, as compiled Cypher's `drivine_cypher` does, would otherwise be
        turned away at once where another connection wrote after it began to read, without waiting for the lock.
        """
        if self.in_transaction:
            return run()
        self.begin()
        try:
            done = run()
            self.commit()
        except BaseException:
            self.rollback()
            raise
        return done

    def execute(self, statement: str, parameters: Mapping[str, Any] | Sequence[Any]) -> Rows:
        """
        Runs the statement with its values bound (see `bindable`), and gives its rows to read. A failure inside a
        function Drivine registered is raised as itself, as apsw raises it. A text of more than one statement is
        refused before any runs (see `_one_statement`).
        """
        sql, values = bindable(statement, parameters)
        cursor = self.connection.cursor()
        cursor.exec_trace = _one_statement(sql)
        return Rows(cursor.execute(sql, values))

    def fetch(self, statement: str, found: Rows, count: int) -> list[dict[str, Any]]:
        """The next `count` rows at most."""
        return rows(statement, found, list(itertools.islice(found.cursor, count)))

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
        except apsw.Error:
            if self.connection.in_transaction:
                raise
        finally:
            if not self.connection.in_transaction:
                self._ended()

    def ensure(self, graph: SqliteGraph) -> None:
        """
        Makes the graph's tables where they are not all there, each statement on its own: a user who may only read the
        file is refused a `CREATE … IF NOT EXISTS` even over a table that is there, so the store is looked for first
        (see SqliteGraph.ensure). A file an earlier Drivine made, whose ids could be reused, is refused with
        ReusedIdsError.
        """
        graph.ensure(self._first_value)

    def _first_value(self, statement: str) -> Any:
        """The first value of the statement's first row; None where it gives none."""
        cursor = self.connection.execute(statement)
        try:
            row = cursor.fetchone()
        finally:
            cursor.close()
        return None if row is None else row[0]

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


def _one_statement(statement: str) -> Callable[[apsw.Cursor, str, Any], bool]:
    """
    A trace for apsw to call as it runs each statement of `statement`, which refuses it before its first runs where it
    holds another: apsw runs each in turn, where `sqlite3` refused them, so a text given as one statement runs as one.
    """
    first = [True]

    def trace(_cursor: apsw.Cursor, sql: str, _bindings: Any) -> bool:
        if first:
            first.clear()
            if _NO_STATEMENT.fullmatch(statement[len(sql) :]) is None:
                raise DrivineError(
                    "SQLite runs one statement at a time, and this one holds another after its first: send each on "
                    "its own."
                )
        return True

    return trace


class _Values(dict[str, Any]):
    """A statement's named values, as apsw looks each up: one it has and isn't given is refused by its name."""

    def __missing__(self, name: str) -> NoReturn:
        raise DrivineError(f"The statement has a parameter ${name}, and no value is bound to it.")


def bindable(statement: str, parameters: Mapping[str, Any] | Sequence[Any]) -> tuple[str, Any]:
    """
    The statement and its values as apsw binds them. SQLite reads `$name` itself, so a statement's named values
    are bound by name, and one not given is refused by its name; each positional `$1` becomes SQLite's `?1`. A map or
    a list is bound as its JSON text, as Drivine's statements take one; a date as ISO 8601 text, an Enum as its value,
    and so on, as a save writes them (see storable).
    """
    if isinstance(parameters, Mapping):
        return statement, _Values({str(key): _value(value) for key, value in parameters.items()})
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


def rows(statement: str, found: Rows, fetched: list[Any]) -> list[dict[str, Any]]:
    """
    The rows as dicts of their columns. Where the statement opens with JSON_ROWS, each text value is JSON, which is
    read: SQLite has no JSON type for a column to say so.
    """
    if not fetched:
        return []
    names = found.names
    documents = statement.lstrip().startswith(JSON_ROWS)
    return [
        {
            name: (json.loads(value) if documents and isinstance(value, str) else value)
            for name, value in zip(names, row, strict=True)
        }
        for row in fetched
    ]
