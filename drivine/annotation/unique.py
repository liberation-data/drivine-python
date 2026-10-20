from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Unique:
    """A uniqueness constraint over the field, or, as a class argument, over the fields named."""

    properties: Sequence[str] = ()
