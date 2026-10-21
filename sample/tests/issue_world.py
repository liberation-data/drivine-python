"""The issue the sample's DSL tests share."""

from uuid import uuid4

from drivine import StatelessGraphObjectManager
from drivine_sample import IssueCore, Organization, Person, PersonContext, RaisedAndAssignedIssue


async def seed_issue(
    gom: StatelessGraphObjectManager, *, id: int, title: str, body: str, person: str, bio: str, org: str
) -> None:
    """One issue, assigned to and raised by one person who works for one organization, saved by the object manager."""
    someone = Person(uuid=str(uuid4()), name=person, bio=bio)
    await gom.save(
        RaisedAndAssignedIssue(
            issue=IssueCore(uuid=str(uuid4()), id=id, title=title, body=body, state="open", locked=False),
            assigned_to=[someone],
            raised_by=PersonContext(person=someone, works_for=[Organization(uuid=str(uuid4()), name=org)]),
        )
    )
