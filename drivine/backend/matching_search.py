from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from drivine.backend.graph_statement import GraphStatement
from drivine.cypher.shared_compiler import SearchIndex
from drivine.model.graph_view_model import GraphViewModel
from drivine.model.target_model import TargetModel

FILTERED_OVER_FETCH = 10
"""
How many times `limit` hits a full-text index is asked for when something filters the hits after it: the shared
module's `FILTERED_OVER_FETCH`.

Where nothing removes a hit after the index (at most a threshold, which keeps a prefix of the ranking), the best
`limit` hits are the answer, and the index is asked for exactly those. Where something does (a caller's `where`, a
view's required relationship or path, or a root label beyond the first, which the index is over), it is asked for
FILTERED_OVER_FETCH times as many, and the filters keep the best `limit` of them. A filter that passes fewer than one
hit in FILTERED_OVER_FETCH of the best can then return fewer than `limit` rows while the index holds more that pass it.

The bound is not optional: every hit the index yields is collected to scale the scores, so the hits, not the rows
returned, are what a search costs. An ordinary English query matches most of a corpus.
"""

INDEX_LIMIT_CEILING = 2_147_483_647
"""
The most hits a full-text search asks its index for, however large its limit: drivine4j's, a Java int, and the most the
shared module's search is asked for.
"""


@dataclass(frozen=True)
class MatchingSearch:
    """A full-text search, as the backend writes it."""

    label: str
    properties: tuple[str, ...]
    limit: int
    """How many rows are kept. On a graph engine it bounds the hits the index yields too: see FILTERED_OVER_FETCH."""
    where: str | None = None
    threshold: bool = False
    """Whether the hits are held to `$_threshold`."""
    depth: Mapping[str, int] = field(default_factory=lambda: {})
    text: str = field(kw_only=True)
    """The user's query, which the shared module's full-text search reads."""
    index: SearchIndex | None = None
    """What the store's catalog records of the full-text index, where it keeps a record (see `full_text_index`)."""


@dataclass(frozen=True)
class FullTextIndexLookup:
    """The statement that finds what a store's catalog records of a full-text index, and what its rows say of it."""

    statement: GraphStatement
    found: Callable[[Sequence[Any]], SearchIndex]


def searched_labels(target: TargetModel, label: str) -> list[str]:
    """
    The labels a full-text search's hits must have, as the shared module's search is asked for them: the label of the
    index, `label`, first, then the root's others. The hits are narrowed by them before the scale, so the best of it is
    a hit the search can return.
    """
    root = target.root if isinstance(target, GraphViewModel) else target
    return [label, *(it for it in root.labels if it != label)]
