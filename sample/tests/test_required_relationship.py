"""
drivine4j's StatelessRequiredRelationshipTest: a view whose single relationship is required (typed without None) loads
only the roots that have it; one whose relationship is optional loads every root.
"""

from dataclasses import dataclass
from uuid import uuid4

import pytest

from drivine import StatelessGraphObjectManager
from drivine_sample import (
    AnonymousGuideUser,
    AnonymousWebUser,
    GuideUser,
    GuideUserWithOptionalWebUser,
    GuideUserWithRequiredWebUser,
    RegisteredWebUser,
    WebUser,
)
from drivine_sample.sample_refs import refs


@dataclass
class Guides:
    with_anonymous: str
    without_web_user: str


@pytest.fixture
async def guides(gom: StatelessGraphObjectManager) -> Guides:
    with_anonymous, without_web_user = str(uuid4()), str(uuid4())

    def guide(uuid: str, progress: int, web_user: WebUser | None = None) -> GuideUserWithOptionalWebUser:
        return GuideUserWithOptionalWebUser(core=GuideUser(uuid=uuid, guide_progress=progress), web_user=web_user)

    def registered(name: str, email: str) -> RegisteredWebUser:
        return RegisteredWebUser(uuid=str(uuid4()), display_name=name, email=email)

    await gom.save_all(
        [
            guide(str(uuid4()), 10, registered("Basic User", "basic@example.com")),
            guide(
                with_anonymous,
                20,
                AnonymousWebUser(uuid=str(uuid4()), display_name="Anon User", anonymous_token="token123"),
            ),
            guide(str(uuid4()), 30, registered("Registered User", "user@example.com")),
            guide(without_web_user, 0),
        ]
    )
    return Guides(with_anonymous, without_web_user)


async def test_loads_every_root_through_an_optional_relationship(gom: StatelessGraphObjectManager, guides: Guides):
    results = await gom.load_all(
        GuideUserWithOptionalWebUser, refs(GuideUserWithOptionalWebUser).core.guide_progress.gte(0)
    )
    assert len(results) == 4
    assert next(it for it in results if it.core.uuid == guides.without_web_user).web_user is None
    assert len([it for it in results if it.web_user is not None]) == 3


async def test_loads_only_the_roots_that_have_a_required_relationship(gom: StatelessGraphObjectManager, guides: Guides):
    results = await gom.load_all(GuideUserWithRequiredWebUser)
    assert len(results) == 3
    assert all(it.web_user.display_name for it in results)
    assert guides.without_web_user not in [it.core.uuid for it in results]


async def test_requires_a_relationship_to_a_subclasss_labels(gom: StatelessGraphObjectManager, guides: Guides):
    results = await gom.load_all(AnonymousGuideUser)
    assert [(it.core.uuid, it.web_user.display_name, it.web_user.anonymous_token) for it in results] == [
        (guides.with_anonymous, "Anon User", "token123")
    ]


async def test_combines_a_required_relationship_with_a_condition(gom: StatelessGraphObjectManager, guides: Guides):
    results = await gom.load_all(
        GuideUserWithRequiredWebUser, refs(GuideUserWithRequiredWebUser).core.guide_progress.gte(15)
    )
    assert sorted(it.core.guide_progress for it in results) == [20, 30]


async def test_loads_none_when_no_root_has_the_required_relationship(gom: StatelessGraphObjectManager, guides: Guides):
    await gom.delete_all(WebUser)
    assert await gom.load_all(GuideUserWithRequiredWebUser) == []
