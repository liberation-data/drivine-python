from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class MatchingSearch:
    """A full-text search, as the backend writes it."""

    label: str
    properties: tuple[str, ...]
    limit: int
    where: str | None = None
    threshold: bool = False
    """Whether the hits are held to `$_threshold`."""
    depth: Mapping[str, int] = field(default_factory=lambda: {})
    text: str | None = None
    """The query text, also bound as `$_text`, for a backend that writes its terms into the statement."""
    table: str | None = None
    """The table an index made for the search is over, where the store keeps one (see `search_index_table`)."""
