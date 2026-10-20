"""
drivine4j's StatelessDemoTest: loading views with filters, relationship fragments, a view that is its root's
interface, removing relationships safely, the typed query DSL, and saves that are idempotent. The data is written
through the object manager, so the demo runs on every store.
"""

from dataclasses import dataclass
from uuid import uuid4

import pytest

from drivine import IndexAdvicePolicy, RemovedTargets, StatelessGraphObjectManager
from drivine_sample import (
    Issue,
    IssueCore,
    Organization,
    Person,
    PersonCareer,
    PersonContext,
    RaisedAndAssignedIssue,
    WorkHistory,
)
from drivine_sample.sample_refs import refs


@dataclass
class World:
    acme: Organization
    startup: Organization
    alice: str
    bob: str
    carol: str


def job(target: Organization, start_date: str, role: str, tags: list[str] | None = None) -> WorkHistory:
    return WorkHistory(target=target, start_date=start_date, role=role, tags=tags)


@pytest.fixture
async def world(gom: StatelessGraphObjectManager) -> World:
    gom.index_advice = IndexAdvicePolicy.OFF
    acme = Organization(uuid=str(uuid4()), name="Acme Corp")
    startup = Organization(uuid=str(uuid4()), name="Startup Inc")
    alice, bob, carol = str(uuid4()), str(uuid4()), str(uuid4())

    def career(uuid: str, name: str, bio: str, history: WorkHistory) -> PersonCareer:
        return PersonCareer(person=Person(uuid=uuid, name=name, bio=bio), employment_history=[history])

    await gom.save_all(
        [
            career(
                alice,
                "Alice Engineer",
                "Senior Backend Developer",
                job(acme, "2023-01-15", "Senior Engineer", ["backend", "senior"]),
            ),
            career(bob, "Bob Designer", "UX Lead", job(acme, "2022-06-01", "Lead Designer", ["design", "ux"])),
            career(
                carol, "Carol Founder", "CEO and Founder", job(startup, "2021-03-10", "CEO", ["leadership", "founder"])
            ),
        ]
    )
    return World(acme, startup, alice, bob, carol)


# DEMO 1: loading collections, with typed filters.


async def test_loads_a_collection_of_people_with_their_relationships(gom: StatelessGraphObjectManager, world: World):
    everyone = await gom.load_all(PersonContext)
    assert len(everyone) == 3
    at_acme = [it for it in everyone if any(org.name == "Acme Corp" for org in it.works_for)]
    assert len(at_acme) == 2
    found = next(it for it in at_acme if it.person.name == "Alice Engineer")
    assert found.person.bio == "Senior Backend Developer"
    assert len(found.works_for) == 1


async def test_filters_a_collection_by_a_persons_bio(gom: StatelessGraphObjectManager, world: World):
    leads = await gom.load_all(PersonContext, where=refs(PersonContext).person.bio.contains("Lead"))
    assert [(it.person.name, it.person.bio) for it in leads] == [("Bob Designer", "UX Lead")]


async def test_filters_by_a_property_that_has_no_value(gom: StatelessGraphObjectManager, world: World):
    q = refs(PersonContext)
    await gom.save(
        PersonContext(
            person=Person(uuid=str(uuid4()), name="Dave Minimalist"),
            works_for=[Organization(uuid=str(uuid4()), name="Stealth Startup")],
        )
    )
    without_bio = await gom.load_all(PersonContext, where=q.person.bio.is_null())
    assert [it.person.name for it in without_bio] == ["Dave Minimalist"]
    assert without_bio[0].person.bio is None
    with_bio = await gom.load_all(PersonContext, where=q.person.bio.is_not_null())
    assert len(with_bio) == 3 and all(it.person.bio is not None for it in with_bio)


# DEMO 2: relationship fragments, properties on a relationship.


async def test_loads_a_relationships_properties_with_the_node_it_leads_to(
    gom: StatelessGraphObjectManager, world: World
):
    career = await gom.load(PersonCareer, world.alice)
    assert career is not None and career.person.name == "Alice Engineer"
    [employment] = career.employment_history
    assert (employment.start_date, employment.role, employment.target.name) == (
        "2023-01-15",
        "Senior Engineer",
        "Acme Corp",
    )


async def test_loads_every_career_with_its_relationships_properties(gom: StatelessGraphObjectManager, world: World):
    careers = await gom.load_all(PersonCareer)
    assert len(careers) == 3
    found = next(it for it in careers if it.person.name == "Alice Engineer")
    assert (found.employment_history[0].role, found.employment_history[0].start_date) == (
        "Senior Engineer",
        "2023-01-15",
    )


# DEMO 3: a view that is an Issue itself, handing its root's fields on.


async def test_lets_a_view_stand_for_its_root_through_a_protocol(gom: StatelessGraphObjectManager, world: World):
    issue_id = str(uuid4())
    tester = Person(uuid=str(uuid4()), name="Dave Tester", bio="QA Engineer")
    await gom.save(
        RaisedAndAssignedIssue(
            issue=IssueCore(
                uuid=issue_id, id=999, title="Bug in login", body="Users cannot log in", state="open", locked=False
            ),
            assigned_to=[tester],
            raised_by=PersonContext(person=tester, works_for=[]),
        )
    )
    raised = await gom.load(RaisedAndAssignedIssue, issue_id)
    assert raised is not None and raised.raised_by is not None

    def described(issue: Issue) -> str:
        return f"{issue.title} (state: {issue.state})"

    assert described(raised) == "Bug in login (state: open)"
    assert raised.assigned_to[0].name == "Dave Tester"
    assert raised.raised_by.person.name == "Dave Tester"


# DEMO 4: removing relationships, and what happens to the nodes they led to.


async def test_keeps_the_node_a_removed_relationship_led_to_by_default(gom: StatelessGraphObjectManager, world: World):
    career = await gom.load(PersonCareer, world.carol)
    assert career is not None
    career.employment_history = []
    await gom.save(career, replace=["employment_history"])
    reloaded = await gom.load(PersonCareer, world.carol)
    assert reloaded is not None and reloaded.employment_history == []
    assert await gom.load(Organization, world.startup.uuid) is not None


async def test_deletes_the_node_a_removed_relationship_led_to_when_nothing_else_refers_to_it(
    gom: StatelessGraphObjectManager, world: World
):
    solo = Organization(uuid=str(uuid4()), name="Solo Org")
    solo_alice = str(uuid4())
    await gom.save(
        PersonCareer(
            person=Person(uuid=solo_alice, name="Solo Alice", bio="Only employee"),
            employment_history=[job(solo, "2024-01-01", "Everything")],
        )
    )
    career = await gom.load(PersonCareer, solo_alice)
    assert career is not None
    career.employment_history = []
    await gom.save(career, replace=["employment_history"], removed_targets=RemovedTargets.DELETE_UNREFERENCED)
    assert await gom.load(Organization, solo.uuid) is None
    assert await gom.load(Organization, world.acme.uuid) is not None


# DEMO 5: the typed query DSL.


async def test_checks_queries_with_the_typed_references(gom: StatelessGraphObjectManager, world: World):
    results = await gom.load_all(PersonCareer, where=refs(PersonCareer).person.name.eq("Alice Engineer"))
    assert [(it.person.name, it.employment_history[0].role) for it in results] == [
        ("Alice Engineer", "Senior Engineer")
    ]
    # refs(PersonCareer).person.namee, or .person.name.gt(3), is an error to pyright and mypy, from the stub the build
    # writes.


async def test_loads_a_relationship_fragments_list_property_kept_as_json(
    gom: StatelessGraphObjectManager, world: World
):
    career = await gom.load(PersonCareer, world.alice)
    assert career is not None and career.employment_history[0].tags == ["backend", "senior"]
    results = await gom.load_all(PersonCareer, where=refs(PersonCareer).person.name.eq("Alice Engineer"))
    assert results[0].employment_history[0].tags == ["backend", "senior"]


# DEMO 6: saves are idempotent.


async def test_doesnt_make_a_node_again_when_one_is_saved_again(gom: StatelessGraphObjectManager, world: World):
    org = Organization(uuid=str(uuid4()), name="Idempotent Corp")
    for _ in range(5):
        await gom.save(org)
    assert await gom.count(Organization, refs(Organization).uuid.eq(org.uuid)) == 1


async def test_updates_a_node_in_place_when_it_is_saved_again_with_other_values(
    gom: StatelessGraphObjectManager, world: World
):
    id = str(uuid4())
    await gom.save(Person(uuid=id, name="Original Name", bio="v1"))
    await gom.save(Person(uuid=id, name="Updated Name", bio="v2"))
    results = await gom.load_all(Person, refs(Person).uuid.eq(id))
    assert [(it.name, it.bio) for it in results] == [("Updated Name", "v2")]


async def test_doesnt_duplicate_a_view_root_or_its_relationships_when_the_view_is_saved_again(
    gom: StatelessGraphObjectManager, world: World
):
    [career] = await gom.load_all(PersonCareer, where=refs(PersonCareer).person.name.eq("Alice Engineer"))
    for _ in range(3):
        await gom.save(career)
    assert await gom.count(Person, refs(Person).uuid.eq(world.alice)) == 1
    reloaded = await gom.load(PersonCareer, world.alice)
    assert reloaded is not None and len(reloaded.employment_history) == 1
