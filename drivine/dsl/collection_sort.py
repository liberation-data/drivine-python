from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class CollectionSort:
    """
    A term that orders a list relationship in each object, not the roots: the list at `relationship` (field names from
    the root, through single relationships on the way), by `key` (field names within each item).
    """

    relationship: tuple[str, ...]
    key: tuple[str, ...]
    direction: Literal["ASC", "DESC"]
    nulls: Literal["FIRST", "LAST"] | None = None
