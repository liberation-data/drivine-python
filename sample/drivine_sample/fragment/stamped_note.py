from __future__ import annotations

from typing import Annotated

from drivine import NodeFragment, NodeId, NodeStamp


class StampedNote(NodeFragment, labels=["StampedNote"]):
    """A note whose stamp is a field: a query over `stamp` reads the property the stamp is stored under."""

    id: Annotated[str, NodeId()]
    text: str
    stamp: Annotated[str | None, NodeStamp()] = None
