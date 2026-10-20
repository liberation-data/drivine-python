"""
drivine4j's OddStoredNameDslTest: a filter and an order read a property by its stored name, whatever that holds: a `$`,
a `%`, a quote or a backslash is part of the name.
"""

from uuid import uuid4

import pytest

from drivine import DatabaseType, IndexAdvicePolicy, StatelessGraphObjectManager
from drivine_sample import OddlyStored
from drivine_sample.sample_refs import refs


async def test_filters_and_orders_by_a_property_whose_stored_name_is_odd(gom: StatelessGraphObjectManager):
    if gom.persistence_manager.type is DatabaseType.FALKORDB:
        pytest.skip("FalkorDB takes a map key only as a plain name, and Drivine refuses any other rather than risk it.")
    gom.index_advice = IndexAdvicePolicy.OFF
    batch = str(uuid4())

    def odd(id: str, price: float, rate: float, greeting: str) -> OddlyStored:
        return OddlyStored(id=f"{batch}-{id}", price=price, rate=rate, greeting=greeting, path=batch)

    await gom.save_all([odd("a", 30, 3, "hello"), odd("b", 10, 1, "hey"), odd("c", 20, 2, "hello")])
    q = refs(OddlyStored)  # `path` is a reference's own attribute: the field is reached with field("path")
    by_price = await gom.load_all(OddlyStored, q.field("path").eq(batch), order_by=q.price.asc())
    assert [(it.price, it.rate) for it in by_price] == [(10, 1), (20, 2), (30, 3)]
    greeted = await gom.load_all(OddlyStored, [q.field("path").eq(batch), q.greeting.eq("hello"), q.rate.gte(3)])
    assert greeted == [odd("a", 30, 3, "hello")]
