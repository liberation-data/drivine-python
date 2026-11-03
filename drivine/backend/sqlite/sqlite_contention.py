from __future__ import annotations

import importlib
import sqlite3

_BUSY = 5
_LOCKED = 6
"""SQLite's result codes for a locked file and a locked table."""


def _errors() -> tuple[type[Exception], ...]:
    """The errors of Python's `sqlite3` module and, where it is installed, of `pysqlite3`, which Drivine may use."""
    try:
        return (sqlite3.Error, importlib.import_module("pysqlite3.dbapi2").Error)
    except ImportError:
        return (sqlite3.Error,)


ERRORS = _errors()
"""What a SQLite statement fails with, from whichever module opened the connection (see sqlite_module)."""

_CONTENTION = ("database is locked", "database table is locked")
"""What SQLite says of a lock, in a message that has lost its code."""


def locked(failure: BaseException) -> bool:
    """
    Whether SQLite turned a statement away for another's lock: the file was locked by another connection for longer
    than the connection waits (`SQLITE_BUSY`), or a table by another statement (`SQLITE_LOCKED`); or a statement outside
    a transaction read before another connection wrote, and may not write over it. Told by the error's code, or by what
    SQLite says of it.
    """
    if not isinstance(failure, ERRORS):
        return False
    code = getattr(failure, "sqlite_errorcode", None)
    if isinstance(code, int) and code & 0xFF in (_BUSY, _LOCKED):
        return True
    return any(message in str(failure) for message in _CONTENTION)


def contended(failure: BaseException) -> bool:
    """Whether the failure, or a cause under it, as deep as ten, is SQLite's turning a statement away for a lock."""
    cause: BaseException | None = failure
    for _ in range(10):
        if cause is None:
            return False
        if locked(cause):
            return True
        cause = cause.__cause__
    return False
