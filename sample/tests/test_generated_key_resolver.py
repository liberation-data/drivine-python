"""
drivine4j's GeneratedKeyResolverTest, which checks the key resolver its code generator writes for a fragment with both a
renamed property and a property bag. Here the references resolve the same keys from the model, so the queries are the
test: a renamed field reads its stored name, and a bag's entries read the keys under its prefix.
"""

import pytest

from drivine import StatelessGraphObjectManager
from drivine_sample import SampleResolverNode
from drivine_sample.sample_refs import refs

q = refs(SampleResolverNode)


def node(id: str, section_id: str, tier: str) -> SampleResolverNode:
    return SampleResolverNode(id=id, section_id=section_id, metadata={"tier": tier})


@pytest.fixture(autouse=True)
async def nodes(gom: StatelessGraphObjectManager) -> None:
    await gom.save_all([node("r1", "s1", "gold"), node("r2", "s2", "silver")])


async def test_filters_by_a_renamed_field_through_its_stored_name(gom: StatelessGraphObjectManager):
    found = await gom.load_all(SampleResolverNode, q.section_id.eq("s2"))
    assert [(it.id, it.section_id, it.metadata["tier"]) for it in found] == [("r2", "s2", "silver")]


async def test_filters_by_a_bags_entry_under_the_bags_prefix(gom: StatelessGraphObjectManager):
    found = await gom.load_all(SampleResolverNode, q.metadata.tier.eq("gold"))
    assert [it.id for it in found] == ["r1"]
