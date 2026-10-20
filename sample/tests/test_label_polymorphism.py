"""
drivine4j's StatelessLabelPolymorphismTest: a related node loads as the subclass its labels say, a view can require one
subclass, instance_of filters by subclass at query time, and a save writes the labels of the object's own class.
"""

from dataclasses import dataclass
from uuid import uuid4

import pytest

from drivine import StatelessGraphObjectManager, any_of
from drivine_sample import (
    AnonymousGuideUser,
    AnonymousWebUser,
    GuideUser,
    GuideUserWithOptionalWebUser,
    GuideUserWithPolymorphicWebUser,
    RegisteredWebUser,
    WebUser,
)
from drivine_sample.sample_refs import refs

q = refs(GuideUserWithPolymorphicWebUser)


def guide(uuid: str, progress: int, web_user: WebUser | None = None) -> GuideUserWithPolymorphicWebUser:
    return GuideUserWithPolymorphicWebUser(core=GuideUser(uuid=uuid, guide_progress=progress), web_user=web_user)


def anonymous(uuid: str, name: str, token: str) -> AnonymousWebUser:
    return AnonymousWebUser(uuid=uuid, display_name=name, anonymous_token=token)


def registered(uuid: str, name: str, email: str) -> RegisteredWebUser:
    return RegisteredWebUser(uuid=uuid, display_name=name, email=email)


@dataclass
class Guides:
    with_anonymous: str
    with_registered: str
    anonymous_id: str


@pytest.fixture
async def guides(gom: StatelessGraphObjectManager) -> Guides:
    with_anonymous, with_registered, anonymous_id = str(uuid4()), str(uuid4()), str(uuid4())
    await gom.save_all(
        [
            guide(with_anonymous, 10, anonymous(anonymous_id, "Anon User", "token-abc")),
            guide(with_registered, 20, registered(str(uuid4()), "Registered User", "test@example.com")),
        ]
    )
    return Guides(with_anonymous, with_registered, anonymous_id)


async def test_loads_a_related_node_as_the_subclass_its_labels_say(gom: StatelessGraphObjectManager, guides: Guides):
    results = await gom.load_all(GuideUserWithPolymorphicWebUser)
    assert len(results) == 2
    anon = next(it for it in results if it.core.uuid == guides.with_anonymous).web_user
    assert isinstance(anon, AnonymousWebUser)
    assert (anon.anonymous_token, anon.uuid) == ("token-abc", guides.anonymous_id)
    reg = next(it for it in results if it.core.uuid == guides.with_registered).web_user
    assert isinstance(reg, RegisteredWebUser) and reg.email == "test@example.com"


async def test_loads_subclasses_through_an_optional_relationship_too(gom: StatelessGraphObjectManager, guides: Guides):
    results = await gom.load_all(
        GuideUserWithOptionalWebUser, refs(GuideUserWithOptionalWebUser).core.guide_progress.gte(0)
    )
    assert isinstance(next(it for it in results if it.core.uuid == guides.with_anonymous).web_user, AnonymousWebUser)
    assert isinstance(next(it for it in results if it.core.uuid == guides.with_registered).web_user, RegisteredWebUser)


async def test_loads_only_the_roots_whose_relationship_leads_to_the_subclass_a_view_requires(
    gom: StatelessGraphObjectManager, guides: Guides
):
    results = await gom.load_all(AnonymousGuideUser)
    assert [(it.core.uuid, it.web_user.anonymous_token, it.web_user.display_name) for it in results] == [
        (guides.with_anonymous, "token-abc", "Anon User")
    ]
    a = refs(AnonymousGuideUser)
    assert [it.core.guide_progress for it in await gom.load_all(AnonymousGuideUser, a.core.guide_progress.gte(5))] == [
        10
    ]
    assert await gom.load_all(AnonymousGuideUser, a.core.guide_progress.gte(100)) == []


async def test_filters_by_subclass_at_query_time_with_instance_of(gom: StatelessGraphObjectManager, guides: Guides):
    anon = await gom.load_all(GuideUserWithPolymorphicWebUser, q.web_user.instance_of(AnonymousWebUser))
    assert [it.core.uuid for it in anon] == [guides.with_anonymous]
    reg = await gom.load_all(
        GuideUserWithPolymorphicWebUser, [q.core.guide_progress.gte(5), q.web_user.instance_of(RegisteredWebUser)]
    )
    assert [it.core.uuid for it in reg] == [guides.with_registered]
    none = await gom.load_all(
        GuideUserWithPolymorphicWebUser, [q.core.guide_progress.gte(100), q.web_user.instance_of(AnonymousWebUser)]
    )
    assert none == []
    either = await gom.load_all(
        GuideUserWithPolymorphicWebUser,
        any_of(q.web_user.instance_of(AnonymousWebUser), q.web_user.instance_of(RegisteredWebUser)),
    )
    assert len(either) == 2


async def test_saves_a_related_node_with_its_own_classs_labels(gom: StatelessGraphObjectManager, guides: Guides):
    guide_id, web_user_id = str(uuid4()), str(uuid4())
    await gom.save(guide(guide_id, 50))
    [loaded] = await gom.load_all(GuideUserWithPolymorphicWebUser, q.core.uuid.eq(guide_id))
    assert loaded.web_user is None
    loaded.web_user = anonymous(web_user_id, "New Anonymous User", "new-token-xyz")
    await gom.save(loaded)
    assert isinstance(await gom.load(AnonymousWebUser, web_user_id), AnonymousWebUser)
    found = await gom.load_all(
        GuideUserWithPolymorphicWebUser, [q.core.uuid.eq(guide_id), q.web_user.instance_of(AnonymousWebUser)]
    )
    web_user = found[0].web_user
    assert isinstance(web_user, AnonymousWebUser) and web_user.anonymous_token == "new-token-xyz"


async def test_saves_each_subclass_with_its_labels(gom: StatelessGraphObjectManager, guides: Guides):
    first, second, anon_id, reg_id = str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4())
    await gom.save_all([guide(first, 10), guide(second, 20)])
    found = await gom.load_all(GuideUserWithPolymorphicWebUser, any_of(q.core.uuid.eq(first), q.core.uuid.eq(second)))
    one = next(it for it in found if it.core.uuid == first)
    two = next(it for it in found if it.core.uuid == second)
    one.web_user = anonymous(anon_id, "Anon", "anon-token")
    two.web_user = registered(reg_id, "Registered", "reg@example.com")
    await gom.save(one)
    await gom.save(two)
    assert await gom.load(AnonymousWebUser, anon_id) is not None
    assert await gom.load(RegisteredWebUser, reg_id) is not None

    def ours(it: GuideUserWithPolymorphicWebUser) -> bool:
        return it.core.uuid in (first, second)

    anon_guides = [
        it
        for it in await gom.load_all(GuideUserWithPolymorphicWebUser, q.web_user.instance_of(AnonymousWebUser))
        if ours(it)
    ]
    reg_guides = [
        it
        for it in await gom.load_all(GuideUserWithPolymorphicWebUser, q.web_user.instance_of(RegisteredWebUser))
        if ours(it)
    ]
    assert ([it.core.uuid for it in anon_guides], [it.core.uuid for it in reg_guides]) == ([first], [second])
