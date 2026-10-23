from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar

from drivine.schema.schema_item import SchemaItem

S = TypeVar("S")

EnsureStatus = Literal["CREATED", "ALREADY_MATCHING", "DRIFT", "VIOLATION", "RECREATED"]


@dataclass(frozen=True)
class EnsureResult(Generic[S]):
    """
    CREATED: nothing was there, and the item was made. ALREADY_MATCHING: a matching item is there; nothing changed.
    DRIFT: an item is there with another shape (a vector index's dimensions or similarity, a full-text index's
    properties); nothing changed, and `recreate` replaces it. VIOLATION: a constraint the data already breaks; nothing
    was made. RECREATED: from `recreate`.
    """

    status: EnsureStatus
    spec: S
    sample: tuple[Any, ...] | None = None
    """
    For a VIOLATION: a sample of the keys more than one node holds, each `{"properties": {...}, "count": n, "nodes":
    [...]}`, with up to the sample size of the nodes that hold it: each its `elementId`, the engine's own id (Neo4j's
    `elementId(n)`, Memgraph's and FalkorDB's `id(n)`, the node row's `nid` on Oracle and Postgres), and the spec's
    `node_id_property` where it has one that isn't a key.
    """
    found: SchemaItem | None = None
    """For a DRIFT: what the engine holds."""
