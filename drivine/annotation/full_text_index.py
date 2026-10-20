from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class FullTextIndex:
    """A full-text index over the field, or, as a class argument, over the fields named. Drives `load_matching`."""

    properties: Sequence[str] = ()
