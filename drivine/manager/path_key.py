from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from drivine.annotation.hop import Hop


@dataclass(frozen=True)
class PathKey:
    """Groups by the node of a fragment class the source reaches through the hops. The key comes back as that node."""

    target: type
    hops: Sequence[Hop]
