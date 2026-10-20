"""drivine4j's StatelessSimpleGeneratedDslTest: the query DSL works at all."""

import pytest

from drivine import StatelessGraphObjectManager
from drivine_sample import RaisedAndAssignedIssue
from drivine_sample.sample_refs import refs
from tests.issue_world import seed_issue


@pytest.fixture(autouse=True)
async def issue(gom: StatelessGraphObjectManager) -> None:
    await seed_issue(
        gom, id=1, title="Test Issue", body="Test body", person="Test Person", bio="Test bio", org="Test Org"
    )


async def test_loads_all_issues_without_a_filter(gom: StatelessGraphObjectManager):
    assert len(await gom.load_all(RaisedAndAssignedIssue)) == 1


async def test_filters_with_the_dsl(gom: StatelessGraphObjectManager):
    assert len(await gom.load_all(RaisedAndAssignedIssue, refs(RaisedAndAssignedIssue).issue.state.eq("open"))) == 1
