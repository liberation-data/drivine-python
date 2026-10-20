"""
drivine4j's StatelessPatchDslTest and StatelessPatchTransactionTest: a patch of a fragment or of a view's root by id,
where a condition holds (on a view, one over its relationships), and a patch that joins a transaction rolled back.
"""

from uuid import uuid4

import pytest

from drivine import DatabaseType, PatchResult, StatelessGraphObjectManager, transaction
from drivine_sample import IssueCore, Person, PersonContext, RaisedAndAssignedIssue, StampedNote
from drivine_sample.sample_refs import refs

CHANGED = PatchResult(found=True, applied=True, changed=True)
UNMET = PatchResult(found=True, applied=False, changed=False)
ABSENT = PatchResult(found=False, applied=False, changed=False)


async def test_patches_a_fragment_by_its_id_where_it_meets_the_condition(gom: StatelessGraphObjectManager):
    saved = await gom.save(StampedNote(id=str(uuid4()), text="Call Ada"))

    async def patch_text(to: str, where: str) -> PatchResult:
        return await gom.patch(StampedNote, saved.id, set={"text": to}, where=refs(StampedNote).text.eq(where))

    assert [await patch_text("Call Bob", "Call Cy"), await patch_text("Call Bob", "Call Ada")] == [UNMET, CHANGED]
    stored = await gom.load(StampedNote, saved.id)
    assert stored is not None and stored.text == "Call Bob" and stored.stamp != saved.stamp
    assert await gom.patch(StampedNote, str(uuid4()), set={"text": "nobody"}) == ABSENT


async def test_patches_a_views_root_where_its_relationship_meets_the_condition(gom: StatelessGraphObjectManager):
    uuid = str(uuid4())
    ada = Person(uuid=str(uuid4()), name="Ada")
    await gom.save(
        RaisedAndAssignedIssue(
            issue=IssueCore(uuid=uuid, id=7, title="As raised", locked=False),
            assigned_to=[ada],
            raised_by=PersonContext(person=ada, works_for=[]),
        )
    )

    async def retitle(title: str, assignee: str) -> PatchResult:
        return await gom.patch(
            RaisedAndAssignedIssue,
            uuid,
            set={"title": title},
            where=refs(RaisedAndAssignedIssue).assigned_to.name.eq(assignee),
        )

    assert await retitle("For Bob", "Bob") == UNMET
    assert await retitle("For Ada", "Ada") == CHANGED
    stored = await gom.load(IssueCore, uuid)
    assert stored is not None and stored.title == "For Ada"


async def test_undoes_a_patch_with_the_transaction_it_joined(gom: StatelessGraphObjectManager):
    if gom.persistence_manager.type is DatabaseType.FALKORDB:
        pytest.skip("FalkorDB has no transactions: nothing there is rolled back.")
    id = str(uuid4())
    saved = await gom.save(StampedNote(id=id, text="Call Ada"))
    async with transaction(rollback=True):
        result = await gom.patch(StampedNote, id, set={"text": "Call Bob"})
        # The patch is seen inside the transaction it joined.
        inside = await gom.load(StampedNote, id)
        assert inside is not None and inside.text == "Call Bob"
    assert result == CHANGED
    stored = await gom.load(StampedNote, id)
    assert stored is not None and (stored.text, stored.stamp) == ("Call Ada", saved.stamp)
