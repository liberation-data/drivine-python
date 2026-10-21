from __future__ import annotations

from typing import Annotated

from drivine import NodeFragment, NodeId


class GuideUser(NodeFragment, labels=["GuideUser"]):
    """A user of a guide, which may be linked to a WebUser by IS_WEB_USER."""

    uuid: Annotated[str, NodeId()]
    guide_progress: int = 0
