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
