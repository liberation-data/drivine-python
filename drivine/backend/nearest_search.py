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
    table: str | None = None
    """
    The table an index made for the search is over, where the store keeps one (see `search_index_table`). On Postgres,
    whose index is over the node table, the size of the embeddings the index holds.
    """
    dimensions: int | None = None
    """The size of the vector searched for: a store that measures only embeddings of one size reads only those."""
