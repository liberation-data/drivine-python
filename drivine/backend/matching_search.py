from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

FILTERED_OVER_FETCH = 10
"""How many times `limit` hits a full-text index is asked for when something filters the hits after it."""

INDEX_LIMIT_CEILING = 2_147_483_647
"""The most hits a full-text search asks its index for, however large its limit: drivine4j's, a Java int."""


def index_limit(limit: int, filtered: bool) -> int:
    """
    How many hits a full-text index call may yield for a search that keeps `limit` rows.

    Where nothing removes a hit after the index (at most a threshold, which keeps a prefix of the ranking), the best
    `limit` hits are the answer, and the index is asked for exactly those. Where something does (a caller's `where`, a
    view's required relationship or path, or a root label beyond the first, which the index is over), it is asked for
    FILTERED_OVER_FETCH times as many, and the filters keep the best `limit` of them. A filter that passes fewer than
    one hit in FILTERED_OVER_FETCH of the best can then return fewer than `limit` rows while the index holds more that
    pass it.

    The bound is not optional: every hit the index yields is collected to scale the scores, and each row after that
    carries the collection, so the hits, not the rows returned, are what a search costs. An ordinary English query
    matches most of a corpus.
    """
    return min(limit * FILTERED_OVER_FETCH if filtered else limit, INDEX_LIMIT_CEILING)


@dataclass(frozen=True)
class MatchingSearch:
    """A full-text search, as the backend writes it."""

    label: str
    properties: tuple[str, ...]
    limit: int
    """How many rows are kept. On a graph engine it bounds the hits the index yields too: see `index_limit`."""
    where: str | None = None
    threshold: bool = False
    """Whether the hits are held to `$_threshold`."""
    depth: Mapping[str, int] = field(default_factory=lambda: {})
    text: str | None = None
    """The query text, also bound as `$_text`, for a backend that writes its terms into the statement."""
    table: str | None = None
    """The table an index made for the search is over, where the store keeps one (see `search_index_table`)."""
