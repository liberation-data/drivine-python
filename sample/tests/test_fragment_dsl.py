"""
drivine4j's StatelessFragmentDslTest: the query DSL over a bare fragment, with load_all, count and delete_all, a
condition on the node's own properties, and instance_of over a fragment its subclasses extend.
"""

from uuid import uuid4

import pytest

from drivine import StatelessGraphObjectManager
from drivine_sample import AnonymousWebUser, RegisteredWebUser, WebUser
from drivine_sample.sample_refs import refs

ANON_TOKEN = "anon-tok"
REG_EMAIL = "reg@example.com"


@pytest.fixture(autouse=True)
async def users(gom: StatelessGraphObjectManager) -> None:
    await gom.save_all(
        [
            AnonymousWebUser(uuid=str(uuid4()), display_name="Anon", anonymous_token=ANON_TOKEN),
            RegisteredWebUser(uuid=str(uuid4()), display_name="Reg", email=REG_EMAIL),
        ]
    )


async def test_loads_each_node_of_a_fragment_as_its_subclass(gom: StatelessGraphObjectManager):
    found = await gom.load_all(WebUser, refs(WebUser).display_name.neq("nobody"))
    assert len(found) == 2
    assert any(isinstance(it, AnonymousWebUser) for it in found) and any(
        isinstance(it, RegisteredWebUser) for it in found
    )


async def test_filters_a_fragment_to_one_subclass_with_instance_of(gom: StatelessGraphObjectManager):
    anonymous = await gom.load_all(WebUser, refs(WebUser).instance_of(AnonymousWebUser))
    assert len(anonymous) == 1 and isinstance(anonymous[0], AnonymousWebUser)


async def test_filters_by_a_subclasss_own_property_loading_the_subclass(gom: StatelessGraphObjectManager):
    q = refs(RegisteredWebUser)
    assert [it.email for it in await gom.load_all(RegisteredWebUser, q.email.eq(REG_EMAIL))] == [REG_EMAIL]
    assert await gom.load_all(RegisteredWebUser, q.email.eq("missing@example.com")) == []


async def test_counts_and_deletes_what_the_dsl_selects(gom: StatelessGraphObjectManager):
    assert await gom.count(WebUser, refs(WebUser).display_name.neq("nobody")) == 2
    assert await gom.count(AnonymousWebUser, refs(AnonymousWebUser).anonymous_token.eq(ANON_TOKEN)) == 1
    q = refs(RegisteredWebUser)
    assert await gom.delete_all(RegisteredWebUser, q.email.eq(REG_EMAIL)) == 1
    assert await gom.load_all(RegisteredWebUser, q.email.eq(REG_EMAIL)) == []
