"""
What the sync API asks of threading: the counterpart of drivine/concurrency.py, written by hand, as the one module
the generator doesn't write. Each thread has its own context, so its own current transaction.
"""

from __future__ import annotations

import inspect
import threading
import time
from collections.abc import Callable
from typing import Any, TypeAlias

Lock: TypeAlias = threading.Lock
"""A lock: what queues a transaction's statements on its connection."""

FUNCTIONS = "a plain function"
"""What `@transactional` takes, in its refusal of anything else."""

OTHER_API = "drivine"
"""Where `@transactional` for the other kind of function is."""


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def runs_here(function: Callable[..., Any]) -> bool:
    """Whether `@transactional` from this API can wrap the function: one that isn't a coroutine function."""
    return not inspect.iscoroutinefunction(function)
