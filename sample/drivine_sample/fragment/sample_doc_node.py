from __future__ import annotations

from typing import Annotated

from drivine import NodeFragment, NodeId, VectorIndex


class SampleDocNode(NodeFragment, labels=["SampleDoc"]):
    """A fragment with a vector index, which load_nearest searches."""

    id: Annotated[str, NodeId()]
    title: str
    embedding: Annotated[list[float] | None, VectorIndex(similarity="COSINE")] = None
