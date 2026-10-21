"""
drivine4j's StatelessDeleteDslTest: delete_all and delete by id with conditions written in the query DSL, on the root,
a relationship and a nested view. A delete takes the root and its relationships, never the related nodes.
"""

from dataclasses import dataclass
from uuid import uuid4

import pytest

from drivine import StatelessGraphObjectManager
from drivine_sample import IssueCore, Organization, Person, PersonContext, RaisedAndAssignedIssue
from drivine_sample.sample_refs import refs

q = refs(RaisedAndAssignedIssue)


@dataclass
class Seeded:
    open_issue: str
    person_id: str


@pytest.fixture
async def seeded(gom: StatelessGraphObjectManager) -> Seeded:
    open_issue, person_id = str(uuid4()), str(uuid4())
    person = Person(uuid=person_id, name="Test Person", bio="Test bio")
    raised_by = PersonContext(person=person, works_for=[Organization(uuid=str(uuid4()), name="Test Org")])

    def issue(uuid: str, id: int, title: str, state: str, locked: bool) -> RaisedAndAssignedIssue:
        return RaisedAndAssignedIssue(
            issue=IssueCore(uuid=uuid, id=id, title=title, body=title, state=state, locked=locked),
            assigned_to=[person],
            raised_by=raised_by,
        )

    await gom.save_all(
        [
            issue(open_issue, 1, "Open Issue 1", "open", False),
            issue(str(uuid4()), 2, "Open Issue 2", "open", True),
            issue(str(uuid4()), 3, "Closed Issue", "closed", False),
        ]
    )
    return Seeded(open_issue, person_id)


async def every(gom: StatelessGraphObjectManager) -> list[RaisedAndAssignedIssue]:
    return await gom.load_all(RaisedAndAssignedIssue)


async def test_deletes_the_issues_of_one_state(gom: StatelessGraphObjectManager, seeded: Seeded):
    assert len(await every(gom)) == 3
    assert await gom.delete_all(RaisedAndAssignedIssue, q.issue.state.eq("closed")) == 1
    assert [it.issue.state for it in await every(gom)] == ["open", "open"]


async def test_deletes_what_several_conditions_select(gom: StatelessGraphObjectManager, seeded: Seeded):
    assert await gom.delete_all(RaisedAndAssignedIssue, [q.issue.state.eq("open"), q.issue.locked.eq(True)]) == 1
    assert sorted(it.issue.state or "" for it in await every(gom)) == ["closed", "open"]


async def test_deletes_by_a_text_condition(gom: StatelessGraphObjectManager, seeded: Seeded):
    assert await gom.delete_all(RaisedAndAssignedIssue, q.issue.title.contains("Open")) == 2
    assert [it.issue.title for it in await every(gom)] == ["Closed Issue"]


async def test_deletes_by_a_related_node_keeping_the_node(gom: StatelessGraphObjectManager, seeded: Seeded):
    assert await gom.delete_all(RaisedAndAssignedIssue, q.assigned_to.name.eq("Test Person")) == 3
    assert await every(gom) == []
    person = await gom.load(Person, seeded.person_id)
    assert person is not None and person.name == "Test Person"


async def test_deletes_by_a_nested_views_relationship(gom: StatelessGraphObjectManager, seeded: Seeded):
    assert await gom.delete_all(RaisedAndAssignedIssue, q.raised_by.works_for.name.eq("Test Org")) == 3
    assert await every(gom) == []


async def test_deletes_every_one_with_no_condition_and_none_when_nothing_matches(
    gom: StatelessGraphObjectManager, seeded: Seeded
):
    assert await gom.delete_all(RaisedAndAssignedIssue, q.issue.state.eq("nonexistent")) == 0
    assert len(await every(gom)) == 3
    assert await gom.delete_all(RaisedAndAssignedIssue) == 3
    assert await every(gom) == []


async def test_deletes_one_by_id_if_it_meets_a_condition(gom: StatelessGraphObjectManager, seeded: Seeded):
    assert await gom.delete(RaisedAndAssignedIssue, seeded.open_issue, where=q.issue.state.eq("closed")) == 0
    kept = await gom.load(RaisedAndAssignedIssue, seeded.open_issue)
    assert kept is not None and kept.issue.state == "open"
    assert await gom.delete(RaisedAndAssignedIssue, seeded.open_issue, where=q.issue.state.eq("open")) == 1
    assert await gom.load(RaisedAndAssignedIssue, seeded.open_issue) is None
    assert len(await every(gom)) == 2


async def test_keeps_the_related_nodes_and_their_own_relationships(gom: StatelessGraphObjectManager, seeded: Seeded):
    await gom.delete_all(RaisedAndAssignedIssue)
    person = await gom.load(Person, seeded.person_id)
    assert person is not None and person.name == "Test Person"
    contexts = await gom.load_all(PersonContext)
    assert [(it.person.name, it.works_for[0].name) for it in contexts] == [("Test Person", "Test Org")]
