from __future__ import annotations

from typing import Any, Protocol


class BagRefs(Protocol):
    """A property bag: each entry, by its key, `q.metadata.source`. Entries hold any type, so they are untyped."""

    def __getattr__(self, key: str, /) -> Any: ...
