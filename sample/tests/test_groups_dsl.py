"""
drivine4j's StatelessGroupsDslTest: grouped reads over the references of a fragment, by a property or a node a path
reaches, with filtered counts, sums, a having and an order.
"""

from uuid import uuid4

from drivine import RelateMode, StatelessGraphObjectManager, count, hop, key, max_, node_ref, path_key, sum_
from drivine_sample import AnonymousWebUser, IssueCore, Person, RegisteredWebUser, WebUser
from drivine_sample.sample_refs import refs

q = refs(IssueCore)


def batch() -> str:
    """Marks one test's issues: each test's are told apart by their title."""
    return f"groups-{uuid4()}"


async def issue(
    gom: StatelessGraphObjectManager, title: str, id: int, state: str | None = None, locked: bool = False
) -> IssueCore:
    return await gom.save(IssueCore(uuid=str(uuid4()), id=id, state=state, title=title, locked=locked))


async def test_groups_issues_by_a_property_with_a_filtered_count_a_having_and_an_order(
    gom: StatelessGraphObjectManager,
):
    title = batch()
    await issue(gom, title, 1, "open", True)
    await issue(gom, title, 2, "open")
    await issue(gom, title, 3, "closed")
    await issue(gom, title, 4)
    states = await gom.groups.load(
        IssueCore,
        where=q.title.eq(title),
        keys={"state": key(q.state)},
        aggregates={"issues": count(), "locked": count(q.locked.eq(True)), "highest": max_(q.id)},
        having=[("highest", "lte", 3)],
        order_by=[("issues", "desc")],
    )
    assert [(it["state"], it["issues"], it["locked"]) for it in states] == [("open", 2, 1), ("closed", 1, 0)]


async def test_groups_issues_by_the_person_they_are_assigned_to_who_loads_as_a_fragment(
    gom: StatelessGraphObjectManager,
):
    title = batch()
    ada = await gom.save(Person(uuid=str(uuid4()), name="Ada"))
    bob = await gom.save(Person(uuid=str(uuid4()), name="Bob"))
    issues = [
        await issue(gom, title, 1, "open"),
        await issue(gom, title, 2, "open"),
        await issue(gom, title, 3, "open"),
    ]
    for assigned, person in [(issues[0], ada), (issues[1], ada), (issues[2], bob), (issues[2], ada)]:
        await gom.edges.relate(
            node_ref(IssueCore, assigned.uuid), node_ref(Person, person.uuid), "ASSIGNED_TO", {}, RelateMode.CREATE
        )
    assigned_to = await gom.groups.load(
        IssueCore,
        where=q.title.eq(title),
        keys={"person": path_key(Person, [hop("ASSIGNED_TO")])},
        aggregates={"open": count(), "numbers": sum_(q.id)},
        order_by=[("open", "desc")],
    )
    assert [(it["person"].name, it["open"], it["numbers"]) for it in assigned_to] == [("Ada", 3, 6), ("Bob", 1, 3)]
    assert isinstance(assigned_to[0]["person"], Person)


async def test_groups_a_fragments_nodes_and_a_subclasss_by_its_own_property(gom: StatelessGraphObjectManager):
    name = batch()
    await gom.save_all(
        [
            AnonymousWebUser(uuid=str(uuid4()), display_name=name, anonymous_token="t1"),
            AnonymousWebUser(uuid=str(uuid4()), display_name=name, anonymous_token="t2"),
            RegisteredWebUser(uuid=str(uuid4()), display_name=name, email="ada@example.com"),
        ]
    )
    w = refs(WebUser)
    every = await gom.groups.load(
        WebUser, where=w.display_name.eq(name), keys={"name": key(w.display_name)}, aggregates={"users": count()}
    )
    assert [(it["name"], it["users"]) for it in every] == [(name, 3)]
    a = refs(AnonymousWebUser)
    anonymous = await gom.groups.load(
        AnonymousWebUser,
        where=a.display_name.eq(name),
        keys={"token": key(a.anonymous_token)},
        aggregates={"users": count()},
    )
    assert [(it["token"], it["users"]) for it in anonymous] == [("t1", 1), ("t2", 1)]
