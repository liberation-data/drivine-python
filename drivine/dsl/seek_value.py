from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SeekValue:
    """A property, and its value at the end of the last page: a keyset cursor's component, for `seek`."""

    path: tuple[str, ...]
    value: Any
