from __future__ import annotations

from typing import Annotated

from drivine import GraphProperty, NodeFragment, NodeId


class ChunkNode(NodeFragment, labels=["ContentElement", "Chunk"]):
    """A field stored under another name: queries over container_section_id read the property of that name."""

    id: Annotated[str, NodeId()]
    text: str
    container_section_id: Annotated[str | None, GraphProperty("container_section_id")] = None
