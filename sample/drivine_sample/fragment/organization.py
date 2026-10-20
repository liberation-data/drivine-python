from __future__ import annotations

from typing import Annotated

from drivine import NodeFragment, NodeId


class Organization(NodeFragment, labels=["Organization"]):
    uuid: Annotated[str, NodeId()]
    name: str
