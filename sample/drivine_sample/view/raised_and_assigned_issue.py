from __future__ import annotations

from typing import Annotated

from drivine import GraphView, Relationship, Root
from drivine_sample.fragment.issue_core import IssueCore
from drivine_sample.fragment.issue_state_reason import IssueStateReason
from drivine_sample.fragment.person import Person
from drivine_sample.view.person_context import PersonContext


class RaisedAndAssignedIssue(GraphView):
    """
    An issue with who it's assigned to and who raised it. It is an Issue itself, handing the root's fields on, as
    drivine4j's `Issue by issue` delegation does.
    """

    issue: Annotated[IssueCore, Root()]
    assigned_to: Annotated[list[Person], Relationship("ASSIGNED_TO")] = []
    raised_by: Annotated[PersonContext | None, Relationship("RAISED_BY")] = None

    @property
    def uuid(self) -> str:
        return self.issue.uuid

    @property
    def id(self) -> int:
        return self.issue.id

    @property
    def state(self) -> str | None:
        return self.issue.state

    @property
    def state_reason(self) -> IssueStateReason | None:
        return self.issue.state_reason

    @property
    def title(self) -> str | None:
        return self.issue.title

    @property
    def body(self) -> str | None:
        return self.issue.body

    @property
    def locked(self) -> bool:
        return self.issue.locked
