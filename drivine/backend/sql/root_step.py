from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class RootStep:
    """What a `node` step says about the save's root."""

    carried: str | None
    """The stamp the object carries, which the stamp handed back is composed from."""
    expect: str | None
    """The stamp, or its data token, the node must carry for the save to go ahead."""
    whole: bool
    """Whether `expect` is the whole stamp rather than its data token."""
    mode: Literal["EITHER", "ABSENT", "FOUND"]
    """Whether the step makes the node when it is not there (EITHER), only when it is not (ABSENT), or never."""
