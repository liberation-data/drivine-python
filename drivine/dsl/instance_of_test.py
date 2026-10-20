from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InstanceOfTest:
    """The node carries all of a fragment class's labels."""

    type: type
