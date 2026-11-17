"""
What the sync API asks of threading: the counterpart of drivine/concurrency.py, written by hand, as the one module
the generator doesn't write. Each thread has its own context, so its own current transaction.
"""

from __future__ import annotations

import contextvars
import inspect
import threading
import time
from collections.abc import Callable
from concurrent.futures import Executor
from typing import Any, TypeAlias, TypeVar

R = TypeVar("R")

Lock: TypeAlias = threading.Lock
"""A lock: what queues a transaction's statements on its connection."""

FUNCTIONS = "a plain function"
"""What `@transactional` takes, in its refusal of anything else."""

OTHER_API = "drivine"
"""Where `@transactional` for the other kind of function is."""


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def blocking(function: Callable[..., R], *arguments: Any, on: Executor | None = None) -> R:
    """
    Runs a call that blocks, such as one on a SQLite connection, where it is; on the executor `on` where one is given,
    as the async API does, and waits for it: a SQLite connection's own thread is started with the stack its functions
    want (see SqliteSession.thread), which the caller's may not have.
    """
    if on is None:
        return function(*arguments)
    return on.submit(contextvars.copy_context().run, function, *arguments).result()


def runs_here(function: Callable[..., Any]) -> bool:
    """Whether `@transactional` from this API can wrap the function: one that isn't a coroutine function."""
    return not inspect.iscoroutinefunction(function)
