"""
What the async API asks of asyncio. drivine.sync is generated from the async source, and this is the one module
written twice: drivine/sync/concurrency.py is its counterpart, over threading.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any, TypeAlias, TypeVar

R = TypeVar("R")

Lock: TypeAlias = asyncio.Lock
"""A lock granted in the order asked: what queues a transaction's statements on its connection."""

FUNCTIONS = "an async function"
"""What `@transactional` takes, in its refusal of anything else."""

OTHER_API = "drivine.sync"
"""Where `@transactional` for the other kind of function is."""


async def sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def blocking(function: Callable[..., R], *arguments: Any) -> R:
    """
    Runs a call that blocks, such as one of the `sqlite3` module, on a thread of its own, so the event loop goes on
    meanwhile. The sync API calls it where it is.
    """
    return await asyncio.to_thread(function, *arguments)


def runs_here(function: Callable[..., Any]) -> bool:
    """Whether `@transactional` from this API can wrap the function: a coroutine function."""
    return inspect.iscoroutinefunction(function)
