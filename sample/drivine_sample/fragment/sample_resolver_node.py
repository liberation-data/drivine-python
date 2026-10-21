from __future__ import annotations

from typing import Annotated, Any

from drivine import GraphProperty, NodeFragment, NodeId, PropertyBag


class SampleResolverNode(NodeFragment, labels=["SampleResolver"]):
    """A promoted field beside a free-form bag: section_id is stored as section_id, bag entries under `metadata.`."""

    id: Annotated[str, NodeId()]
    section_id: Annotated[str | None, GraphProperty("section_id")] = None
    metadata: Annotated[dict[str, Any], PropertyBag(prefix="metadata")] = {}
