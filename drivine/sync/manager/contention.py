# Written by scripts/unasync.py from drivine/manager/contention.py:
# change that, and run the script again.
"""Running a statement again when the engine turned it away because another writer held what it writes."""

from __future__ import annotations

import contextlib
import random
import re
from collections.abc import Callable
from typing import TypeVar

from drivine.sync import concurrency

R = TypeVar("R")
E = TypeVar("E", bound=BaseException)

CONTENDED_ATTEMPTS = 6
"""How many times a statement the engine turns away is run before its first failure is raised."""
CONTENDED_PAUSE_SECONDS = 0.015
"""The least pause after the first attempt that is turned away; each later one is as much longer."""

_DDL_DEADLOCKED = re.compile(r"ORA-(00060|04020)\b")
_DDL_BUSY = re.compile(r"ORA-00054\b")
_POSTGRES_DEADLOCK = "40P01"

_GIVEN_UP = "_drivine_given_up"
"""
The attribute that marks a failure a run of when_not_contended raised: ran out of attempts on, or would not run again.
It is set on the failure itself, so an outer run finds it under the error that wraps it. An attribute, not a weak set:
an exception takes no weak reference.
"""


def contended_pause(attempt: int, chance: Callable[[], float] = random.random) -> float:
    """
    The pause after `attempt` is turned away: as many pauses as attempts so far, and up to as much again at random.
    Two writers turned away by each other would otherwise pause alike, run again together and collide again.
    """
    base = (attempt + 1) * CONTENDED_PAUSE_SECONDS
    return base + chance() * base


def _any_cause(failure: BaseException | None, matches: Callable[[BaseException], bool]) -> bool:
    """Whether `failure`, or a cause under it, as deep as ten, is one `matches` takes."""
    cause = failure
    for _ in range(10):
        if cause is None:
            return False
        if matches(cause):
            return True
        cause = cause.__cause__
    return False


def ddl_deadlocked(failure: BaseException | None) -> bool:
    """
    Whether a SQL store turned schema statements away because two sessions were each waiting on the other, so that
    running them again may succeed. On Oracle, that is for an object's library cache lock (ORA-04020), as DDL over a
    table that other sessions' writes hold may be, or for a row (ORA-00060); on Postgres, a deadlock (40P01). A graph's
    first-use block runs again only for these: a lock merely busy has had its wait inside the block already (on Oracle,
    `OracleGraph.DDL_WAIT_SECONDS`), and running it again would hold the app's first connection for that wait again
    each time. The failure and each cause under it are looked at.
    """
    return _any_cause(
        failure,
        lambda it: bool(_DDL_DEADLOCKED.search(str(it))) or getattr(it, "sqlstate", None) == _POSTGRES_DEADLOCK,
    )


def ddl_contended(failure: BaseException | None) -> bool:
    """
    Whether a SQL store turned schema statements away because of another session's, as sessions that make a store's
    indexes together do, so that running them again may succeed: a deadlock (see ddl_deadlocked), or on Oracle a lock
    that was busy (ORA-00054). The failure and each cause under it are looked at.
    """
    return ddl_deadlocked(failure) or _any_cause(failure, lambda it: bool(_DDL_BUSY.search(str(it))))


def _gave_up_on(failure: BaseException) -> bool:
    """
    Whether `failure`, or a cause under it, is one a run of when_not_contended inside this one already raised, as a
    graph's first-use block is under a schema statement that asked for the first connection.
    """
    return _any_cause(failure, lambda it: getattr(it, _GIVEN_UP, False) is True)


def _giving_up(failure: E) -> E:
    """`failure`, marked as one a run gave up on (see _gave_up_on)."""
    with contextlib.suppress(AttributeError, TypeError):
        setattr(failure, _GIVEN_UP, True)
    return failure


def when_not_contended(
    statement: Callable[[], R],
    contended: Callable[[BaseException], bool],
    sleep: Callable[[float], None] = concurrency.sleep,
) -> R:
    """
    Runs `statement`, again after a pause when the engine turned it away for contention. The first failure is raised
    once attempts run out; a failure of another kind is raised at once. So is one a run inside `statement` already
    raised, having run out of attempts on it or taken it for one not to run again, so that runs one inside the other
    don't multiply their attempts (six by six), nor run again what the inner one would not, as a schema statement
    would the first-use block whose lock stayed busy on Oracle: each of those may wait a minute for its lock (see
    `OracleGraph.DDL_WAIT_SECONDS`).
    """
    first: Exception | None = None
    for attempt in range(CONTENDED_ATTEMPTS):
        try:
            return statement()
        except Exception as failure:
            if _gave_up_on(failure) or not contended(failure):
                if first is not None:
                    raise _giving_up(first) from failure
                _giving_up(failure)
                raise
            first = first or failure
            if attempt < CONTENDED_ATTEMPTS - 1:
                sleep(contended_pause(attempt))
    assert first is not None
    raise _giving_up(first)
