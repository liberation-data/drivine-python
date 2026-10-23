from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, Literal, TypedDict, TypeVar

from drivine.schema.schema_item import SchemaItem

S = TypeVar("S")

EnsureStatus = Literal["CREATED", "ALREADY_MATCHING", "DRIFT", "VIOLATION", "RECREATED", "BLOCKED_BY_INDEX"]


class ViolatingKey(TypedDict):
    """
    A key of a uniqueness constraint that more than one node holds, as a violation's sample gives it: TypeScript's
    `ViolatingKey`, so that a sample reads alike from each library.
    """

    properties: dict[str, Any]
    """The constraint's properties, each with the value the nodes hold alike."""
    count: int
    """How many nodes hold the key, however many of them `nodes` names."""
    nodes: list[dict[str, Any]]
    """Up to the sample size of the nodes that hold it, each its `elementId` and the spec's `node_id_property`."""


@dataclass(frozen=True)
class EnsureResult(Generic[S]):
    """
    CREATED: nothing was there, and the item was made. ALREADY_MATCHING: a matching item is there; nothing changed.
    DRIFT: an item is there with another shape (a vector index's dimensions or similarity, a full-text index's
    properties); nothing changed, and `recreate` replaces it. VIOLATION: a constraint the data already breaks; nothing
    was made. RECREATED: from `recreate`. BLOCKED_BY_INDEX: a uniqueness constraint a plain range index over the same
    label and properties is in the way of, on an engine whose constraint brings its own index (Neo4j); nothing was
    changed. Drop `index`, or ensure with `replace_blocking_index`, which drops it for the constraint.
    """

    status: EnsureStatus
    spec: S
    sample: tuple[ViolatingKey, ...] | None = None
    """
    For a VIOLATION: a sample of the keys more than one node holds, each a `ViolatingKey`, with up to the sample size of
    the nodes that hold it: each its `elementId`, the engine's own id (Neo4j's `elementId(n)`, Memgraph's and
    FalkorDB's `id(n)`, the node row's `nid` on Oracle and Postgres), and the spec's `node_id_property` where it has one
    that isn't a key.
    """
    found: SchemaItem | None = None
    """For a DRIFT: what the engine holds."""
    index: SchemaItem | None = None
    """For a BLOCKED_BY_INDEX: the index in the way, named."""
