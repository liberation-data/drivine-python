"""
drivine4j's StatelessGeneratedDslTest: the query DSL over a view's root, its relationships, a nested view's root and
relationships, with AND, any_of (OR) and the text operators. Every query is checked by pyright, through the typed
references the build writes.
"""

import pytest

from drivine import StatelessGraphObjectManager, Where, any_of
from drivine_sample import PersonContext, RaisedAndAssignedIssue
from drivine_sample.sample_refs import refs
from tests.issue_world import seed_issue

q = refs(RaisedAndAssignedIssue)


@pytest.fixture(autouse=True)
async def issue(gom: StatelessGraphObjectManager) -> None:
    await seed_issue(
        gom,
        id=123,
        title="Implement feature X",
        body="This is a test issue",
        person="Alice Developer",
        bio="Senior Engineer",
        org="Acme Corp",
    )


async def issues(gom: StatelessGraphObjectManager, where: Where) -> list[RaisedAndAssignedIssue]:
    return await gom.load_all(RaisedAndAssignedIssue, where)


def raiser(issue: RaisedAndAssignedIssue) -> PersonContext:
    assert issue.raised_by is not None
    return issue.raised_by


async def test_loads_every_issue_with_its_relationships_without_a_filter(gom: StatelessGraphObjectManager):
    [issue, *rest] = await gom.load_all(RaisedAndAssignedIssue)
    assert rest == []
    assert issue.issue.title == "Implement feature X"
    assert [it.name for it in issue.assigned_to] == ["Alice Developer"]


async def test_filters_by_the_roots_state(gom: StatelessGraphObjectManager):
    assert [it.issue.state for it in await issues(gom, q.issue.state.eq("open"))] == ["open"]


async def test_filters_by_the_roots_id(gom: StatelessGraphObjectManager):
    assert [it.issue.id for it in await issues(gom, q.issue.id.eq(123))] == [123]


async def test_filters_by_a_nested_views_root(gom: StatelessGraphObjectManager):
    results = await issues(gom, q.raised_by.person.name.eq("Alice Developer"))
    assert [raiser(it).person.name for it in results] == ["Alice Developer"]


async def test_filters_by_a_nested_views_relationship(gom: StatelessGraphObjectManager):
    results = await issues(gom, q.raised_by.works_for.name.eq("Acme Corp"))
    assert [raiser(it).works_for[0].name for it in results] == ["Acme Corp"]


async def test_combines_filters_with_and(gom: StatelessGraphObjectManager):
    results = await issues(gom, [q.issue.state.eq("open"), q.issue.locked.eq(False)])
    assert [(it.issue.state, it.issue.locked) for it in results] == [("open", False)]


async def test_loads_any_view_with_the_dsl(gom: StatelessGraphObjectManager):
    [context] = await gom.load_all(PersonContext)
    assert (context.person.name, context.person.bio, context.works_for[0].name) == (
        "Alice Developer",
        "Senior Engineer",
        "Acme Corp",
    )
    by_org = await gom.load_all(PersonContext, refs(PersonContext).works_for.name.eq("Acme Corp"))
    assert [it.person.name for it in by_org] == ["Alice Developer"]


async def test_returns_none_when_nothing_matches(gom: StatelessGraphObjectManager):
    assert await issues(gom, q.issue.state.eq("closed")) == []


async def test_filters_by_the_title(gom: StatelessGraphObjectManager):
    results = await issues(gom, q.issue.title.eq("Implement feature X"))
    assert [it.issue.title for it in results] == ["Implement feature X"]


# any_of (OR)


async def test_ors_conditions_on_the_root_matching_none_or_one(gom: StatelessGraphObjectManager):
    assert await issues(gom, any_of(q.issue.state.eq("closed"), q.issue.locked.eq(True))) == []
    results = await issues(gom, any_of(q.issue.state.eq("open"), q.issue.state.eq("closed")))
    assert [it.issue.state for it in results] == ["open"]


async def test_ors_conditions_on_a_relationship_a_nested_views_root_and_its_relationship(
    gom: StatelessGraphObjectManager,
):
    assigned = await issues(
        gom, any_of(q.assigned_to.name.eq("Alice Developer"), q.assigned_to.name.eq("Bob Developer"))
    )
    assert len(assigned) == 1
    raised = await issues(
        gom, any_of(q.raised_by.person.name.eq("Alice Developer"), q.raised_by.person.name.eq("Bob Developer"))
    )
    assert len(raised) == 1
    org = await issues(
        gom, any_of(q.raised_by.works_for.name.eq("Acme Corp"), q.raised_by.works_for.name.eq("Initech"))
    )
    assert [raiser(it).works_for[0].name for it in org] == ["Acme Corp"]


async def test_combines_and_and_or_across_nested_relationships(gom: StatelessGraphObjectManager):
    results = await issues(
        gom,
        [
            q.issue.locked.eq(False),
            any_of(q.raised_by.person.name.eq("Alice Developer"), q.raised_by.works_for.name.eq("Initech")),
        ],
    )
    assert [(it.issue.locked, raiser(it).person.name) for it in results] == [(False, "Alice Developer")]


# Text operators, and harder boolean shapes.


async def test_matches_text_in_nested_views(gom: StatelessGraphObjectManager):
    assert len(await issues(gom, q.raised_by.person.bio.contains("Engineer"))) == 1
    assert len(await issues(gom, q.raised_by.works_for.name.starts_with("Ac"))) == 1
    assert len(await gom.load_all(PersonContext, refs(PersonContext).person.bio.contains("Senior"))) == 1


async def test_ors_three_conditions_over_the_root_and_nested_views(gom: StatelessGraphObjectManager):
    results = await issues(
        gom,
        any_of(
            q.issue.locked.eq(True),
            q.raised_by.person.name.eq("Alice Developer"),
            q.raised_by.works_for.name.eq("Acme Corp"),
        ),
    )
    assert len(results) == 1


async def test_nests_or_within_and(gom: StatelessGraphObjectManager):
    results = await issues(
        gom, [q.issue.state.eq("open"), any_of(q.issue.id.eq(123), q.assigned_to.name.eq("Alice Developer"))]
    )
    assert [it.issue.state for it in results] == ["open"]
    assert len(await issues(gom, [q.issue.title.contains("feature"), q.issue.state.eq("open")])) == 1


async def test_ors_several_conditions_on_one_nested_relationship_and_across_relationships(
    gom: StatelessGraphObjectManager,
):
    orgs = await issues(
        gom,
        any_of(
            q.raised_by.works_for.name.eq("Acme Corp"),
            q.raised_by.works_for.name.eq("Initech"),
            q.raised_by.works_for.name.eq("Umbrella Corp"),
        ),
    )
    assert len(orgs) == 1
    either = await issues(
        gom, any_of(q.assigned_to.name.eq("Alice Developer"), q.raised_by.person.name.eq("Alice Developer"))
    )
    assert len(either) == 1


async def test_ands_root_conditions_with_an_or_over_different_relationships(gom: StatelessGraphObjectManager):
    results = await issues(
        gom,
        [
            q.issue.state.eq("open"),
            q.issue.locked.eq(False),
            any_of(q.raised_by.works_for.name.eq("Acme Corp"), q.assigned_to.name.eq("Alice Developer")),
        ],
    )
    assert [(it.issue.state, it.issue.locked) for it in results] == [("open", False)]


async def test_ors_text_operators_over_several_fields(gom: StatelessGraphObjectManager):
    results = await issues(
        gom,
        any_of(
            q.issue.title.contains("Implement"),
            q.issue.body.contains("test"),
            q.raised_by.person.bio.contains("Developer"),
        ),
    )
    assert len(results) == 1


async def test_returns_none_when_no_or_condition_holds(gom: StatelessGraphObjectManager):
    results = await issues(
        gom,
        any_of(
            q.issue.state.eq("closed"),
            q.assigned_to.name.eq("Nobody"),
            q.raised_by.works_for.name.eq("NonExistent Corp"),
        ),
    )
    assert results == []


async def test_nests_or_within_or(gom: StatelessGraphObjectManager):
    results = await issues(
        gom,
        any_of(q.issue.state.eq("open"), any_of(q.issue.locked.eq(True), q.assigned_to.name.eq("Alice Developer"))),
    )
    assert len(results) == 1


async def test_serves_a_dashboard_query_open_issues_from_one_team_mine_or_about_a_feature(
    gom: StatelessGraphObjectManager,
):
    results = await issues(
        gom,
        [
            q.issue.state.eq("open"),
            q.raised_by.works_for.name.eq("Acme Corp"),
            any_of(q.assigned_to.name.eq("Alice Developer"), q.issue.title.contains("feature")),
        ],
    )
    assert [raiser(it).works_for[0].name for it in results] == ["Acme Corp"]


async def test_ands_two_conditions_on_one_nested_property(gom: StatelessGraphObjectManager):
    results = await issues(
        gom, [q.raised_by.person.name.eq("Alice Developer"), q.raised_by.person.bio.contains("Engineer")]
    )
    assert len(results) == 1
