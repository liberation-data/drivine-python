from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rows:
    """The rows a path's conditions read: the node's, and the relationship's that a relationship fragment holds."""

    node: str
    rel: str | None = None
