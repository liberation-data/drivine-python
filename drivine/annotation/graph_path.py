from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from drivine.annotation.hop import Hop


@dataclass(frozen=True)
class GraphPath:
    """
    A field of a view holding the nodes at the far end of a fixed list of hops from the root, each once, skipping the
    nodes between: `directors: Annotated[list[Director], GraphPath([hop("ACTED_IN"), hop("DIRECTED_BY")])] = []`.
    Read-only. A single path typed without `None` is required, and a list is with `required=True`.
    """

    hops: Sequence[Hop]
    required: bool = False

    def __hash__(self) -> int:
        return hash((tuple(self.hops), self.required))
