"""
drivine4j's StampFilterDslTest and StampOrderDslTest: a filter, an order and a keyset on a NodeStamp field read the
property the stamp is stored under.
"""

from uuid import uuid4

from drivine import IndexAdvicePolicy, SeekValue, StatelessGraphObjectManager
from drivine_sample import StampedNote
from drivine_sample.sample_refs import refs

q = refs(StampedNote)


async def note(gom: StatelessGraphObjectManager, text: str) -> StampedNote:
    return await gom.save(StampedNote(id=str(uuid4()), text=text))


async def saved_stamps(gom: StatelessGraphObjectManager, batch: str) -> list[str]:
    """Five notes of this test's own batch, and their stamps in ascending order."""
    stamps: list[str] = []
    for i in range(1, 6):
        stamp = (await note(gom, f"{batch} {i}")).stamp
        assert stamp is not None
        stamps.append(stamp)
    return sorted(stamps)


async def test_finds_the_node_that_carries_the_stamp(gom: StatelessGraphObjectManager):
    saved = await note(gom, "Call Ada")
    await note(gom, "Call Bob")
    assert saved.stamp is not None
    assert [it.id for it in await gom.load_all(StampedNote, q.stamp.eq(saved.stamp))] == [saved.id]


async def test_orders_by_the_stamp(gom: StatelessGraphObjectManager):
    gom.index_advice = IndexAdvicePolicy.OFF
    batch = str(uuid4())
    stamps = await saved_stamps(gom, batch)
    ascending = await gom.load_all(StampedNote, q.text.starts_with(batch), order_by=q.stamp.asc())
    descending = await gom.load_all(StampedNote, q.text.starts_with(batch), order_by=q.stamp.desc())
    assert [it.stamp for it in ascending] == stamps
    assert [it.stamp for it in descending] == stamps[::-1]


async def test_pages_through_every_node_once_with_a_keyset_on_the_stamp(gom: StatelessGraphObjectManager):
    gom.index_advice = IndexAdvicePolicy.OFF
    batch = str(uuid4())
    stamps = await saved_stamps(gom, batch)
    paged: list[str] = []
    while True:
        seek: SeekValue | None = q.stamp.after(paged[-1]) if paged else None
        page = await gom.load_all(StampedNote, q.text.starts_with(batch), order_by=q.stamp.asc(), seek=seek, limit=2)
        if not page:
            break
        paged += [it.stamp for it in page if it.stamp is not None]
    assert paged == stamps
