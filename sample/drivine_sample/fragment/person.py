from __future__ import annotations

from typing import Annotated

from drivine import NodeFragment, NodeId


class Person(NodeFragment, labels=["Person", "Mapped"]):
    uuid: Annotated[str, NodeId()]
    name: str
    bio: str | None = None
