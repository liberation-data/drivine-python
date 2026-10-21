from __future__ import annotations

from typing import Annotated

from drivine import NodeFragment, NodeId


class WebUser(NodeFragment, labels=["WebUser"]):
    """
    A web user. A node of it loads as the subclass whose labels it carries: AnonymousWebUser or RegisteredWebUser
    (Python's stand-in for drivine4j's sealed class).
    """

    uuid: Annotated[str, NodeId()]
    display_name: str
