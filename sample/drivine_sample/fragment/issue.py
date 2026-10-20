from __future__ import annotations

from typing import Protocol

from drivine_sample.fragment.issue_state_reason import IssueStateReason


class Issue(Protocol):
    """What any issue offers: an IssueCore, or a view of one that hands its fields on (see RaisedAndAssignedIssue)."""

    @property
    def uuid(self) -> str: ...

    @property
    def id(self) -> int: ...

    @property
    def state(self) -> str | None: ...

    @property
    def state_reason(self) -> IssueStateReason | None: ...

    @property
    def title(self) -> str | None: ...

    @property
    def body(self) -> str | None: ...

    @property
    def locked(self) -> bool: ...
