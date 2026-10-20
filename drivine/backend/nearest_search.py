from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from drivine.schema.vector_index_spec import SimilarityFunction


@dataclass(frozen=True)
class NearestSearch:
    """A vector search, as the backend writes it."""

    label: str
    """The label whose vector index is searched: the fragment's first, or a partition's."""
    property: str
    similarity: SimilarityFunction
    k: int
    """How many the index is asked for. Written into the statement: an engine takes only an integer."""
    where: str | None = None
    threshold: bool = False
    """Whether the hits are held to `$_threshold`."""
    limit: int | None = None
    """How many are kept after the filter, if any are trimmed."""
    depth: Mapping[str, int] = field(default_factory=lambda: {})
