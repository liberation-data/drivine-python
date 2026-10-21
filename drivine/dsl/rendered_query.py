from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from drivine.dsl.collection_sort import CollectionSort


@dataclass(frozen=True)
class RenderedQuery:
    """A query as Cypher over the root, bound to an alias: a predicate, an order, and the values both name."""

    where: str | None = None
    order_by: str | None = None
    limit: int | None = None
    skip: int | None = None
    depth: Mapping[str, int] = field(default_factory=lambda: {})
    bindings: Mapping[str, Any] = field(default_factory=lambda: {})
    ordered: tuple[str, ...] = ()
    """The stored names of the root properties ordered by, in order."""
    collection_sorts: tuple[CollectionSort, ...] = ()
    """Terms that order a list relationship in each object, applied as objects are made."""
    pinned: tuple[str, ...] = ()
    """The root properties the condition holds equal to a value at its top level."""
    paged: bool = False
    """Whether the query orders, skips, limits or seeks."""
    seeks: bool = False
    labels_tested: tuple[str, ...] = ()
    """
    The root's labels the predicate tests itself, as it reads a property through a labelled index: a statement needn't
    test them again, and testing them on their own would have the store read the label instead (Oracle).
    """
