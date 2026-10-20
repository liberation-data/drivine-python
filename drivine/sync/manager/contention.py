# Written by scripts/unasync.py from drivine/manager/contention.py:
# change that, and run the script again.
"""Running a statement again when the engine turned it away because another writer held what it writes."""

from __future__ import annotations

import random
from collections.abc import Callable
from typing import TypeVar

from drivine.sync import concurrency

R = TypeVar("R")

CONTENDED_ATTEMPTS = 6
"""How many times a statement the engine turns away is run before its first failure is raised."""
CONTENDED_PAUSE_SECONDS = 0.015
"""The least pause after the first attempt that is turned away; each later one is as much longer."""


def contended_pause(attempt: int, chance: Callable[[], float] = random.random) -> float:
    """
    The pause after `attempt` is turned away: as many pauses as attempts so far, and up to as much again at random.
    Two writers turned away by each other would otherwise pause alike, run again together and collide again.
    """
    base = (attempt + 1) * CONTENDED_PAUSE_SECONDS
    return base + chance() * base


def when_not_contended(
    statement: Callable[[], R],
    contended: Callable[[BaseException], bool],
    sleep: Callable[[float], None] = concurrency.sleep,
) -> R:
    """
    Runs `statement`, again after a pause when the engine turned it away for contention. The first failure is raised
    once attempts run out; a failure of another kind is raised at once.
    """
    first: Exception | None = None
    for attempt in range(CONTENDED_ATTEMPTS):
        try:
            return statement()
        except Exception as failure:
            if not contended(failure):
                if first is not None:
                    raise first from failure
                raise
            first = first or failure
            if attempt < CONTENDED_ATTEMPTS - 1:
                sleep(contended_pause(attempt))
    assert first is not None
    raise first
