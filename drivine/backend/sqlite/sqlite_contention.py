from __future__ import annotations

import sys
from typing import Any

_BUSY = 5
_LOCKED = 6
"""SQLite's result codes for a locked file and a locked table."""


def _apsw() -> Any:
    """
    apsw, through which Drivine opens a SQLite file, where it has been imported: None where it hasn't, as no SQLite
    error can have been raised then. So what tells one apart is read without apsw installed, as by a store of another
    kind.
    """
    return sys.modules.get("apsw")


def sqlite_error(failure: BaseException) -> bool:
    """Whether the failure is SQLite's: an error of apsw's."""
    apsw = _apsw()
    return apsw is not None and isinstance(failure, apsw.Error)


def result_code(failure: BaseException) -> int | None:
    """
    The extended result code SQLite gave with the failure, such as 517 for `SQLITE_BUSY_SNAPSHOT`; None for any other
    failure, or one raised with no code.
    """
    if not sqlite_error(failure):
        return None
    code = getattr(failure, "extendedresult", None)
    return code if isinstance(code, int) else None


_CONTENTION = ("database is locked", "database table is locked")
"""What SQLite says of a lock, in a message that has lost its code."""


def locked(failure: BaseException) -> bool:
    """
    Whether SQLite turned a statement away for another's lock: the file was locked by another connection for longer
    than the connection waits (`SQLITE_BUSY`), or a table by another statement (`SQLITE_LOCKED`); or a statement outside
    a transaction read before another connection wrote, and may not write over it. Told by the error's kind or code, or
    by what SQLite says of it.
    """
    apsw = _apsw()
    if apsw is None or not isinstance(failure, apsw.Error):
        return False
    if isinstance(failure, (apsw.BusyError, apsw.LockedError)):
        return True
    code = result_code(failure)
    if code is not None and code & 0xFF in (_BUSY, _LOCKED):
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
