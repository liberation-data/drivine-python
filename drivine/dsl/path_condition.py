from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from drivine.dsl.condition import Test


@dataclass(frozen=True)
class PathCondition:
    """A test at the end of a path of field names from the query's root, or from an `any`'s node."""

    path: tuple[str, ...]
    test: Test
