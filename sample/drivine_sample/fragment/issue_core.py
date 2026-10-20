from __future__ import annotations

from typing import Annotated

from drivine import NodeFragment, NodeId
from drivine_sample.fragment.issue_state_reason import IssueStateReason


class IssueCore(NodeFragment, labels=["Issue"]):
    """An issue: an Issue itself."""

    uuid: Annotated[str, NodeId()]
    id: int
    state: str | None = None
    state_reason: IssueStateReason | None = None
    title: str | None = None
    body: str | None = None
    locked: bool = False
