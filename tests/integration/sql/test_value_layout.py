"""
A save's values as store layout 2 holds them on each SQL store, written by the shared module and read back through the
object manager (see value_layout): a property whose name begins with `$`, the floats JSON writes no number of beside a
date, one float alike with a date beside it or without, NaN refused by name where the store holds it as no number, and
bytes the list of their signed values. host.yaml's "Values a save writes" pins what the module writes.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime
from typing import Annotated, Any

import pytest

from drivine import (
    DatabaseType,
    DrivineError,
    GraphProperty,
    NodeFragment,
    NodeId,
    PersistenceManager,
    QuerySpecification,
    StatelessGraphObjectManager,
    cypher_statement,
)


class Held(NodeFragment, labels=["Held"]):
    id: Annotated[str, NodeId()]
    dollar: Annotated[int | None, GraphProperty("$dollar")] = None
    born: date | None = None
    at: datetime | None = None
    reading: float | None = None
    others: list[float] | None = None
    raw: bytes | None = None


async def stored(pm: PersistenceManager, id: str) -> dict[str, Any]:
    """The node's properties as the store holds them, as JSON."""
    if pm.type is DatabaseType.ORACLE:
        text = "SELECT JSON_SERIALIZE(props) AS props FROM drivine_node WHERE JSON_VALUE(props, '$.id') = $id"
    elif pm.type is DatabaseType.SQLITE:
        text = "SELECT props FROM drivine_node WHERE props ->> '$.id' = $id"
    else:
        text = "SELECT props::text AS props FROM drivine_node WHERE props ->> 'id' = $id"
    [row] = await pm.query(QuerySpecification[Any](text).bind({"id": id}))
    props = row["props"] if isinstance(row, dict) else row
    found: dict[str, Any] = json.loads(props) if isinstance(props, str) else props
    return found


async def test_a_property_whose_name_begins_with_a_dollar_is_held_escaped_and_read_back_as_it_was(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    await gom.save(Held(id="d1", dollar=7))
    assert (await stored(pm, "d1"))["$$dollar"] == 7
    loaded = await gom.load(Held, "d1")
    assert loaded is not None and loaded.dollar == 7


async def test_minus_zero_and_an_infinity_beside_a_date_are_read_back_as_the_floats_they_are(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    if pm.type is not DatabaseType.SQLITE:
        pytest.skip("Postgres's jsonb holds no infinity, and Oracle's saves hold none until drivine4j#830")
    await gom.save(Held(id="f1", born=date(2024, 1, 1), reading=-0.0, others=[math.inf, -math.inf, 1.5]))
    held = await stored(pm, "f1")
    assert held["born"]["$t"] == "date"
    loaded = await gom.load(Held, "f1")
    assert loaded is not None and loaded.born == date(2024, 1, 1)
    assert loaded.reading == 0 and math.copysign(1, loaded.reading) < 0
    assert loaded.others == [math.inf, -math.inf, 1.5]


async def test_a_float_is_held_alike_with_a_date_beside_it_or_without(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    await gom.save(Held(id="w1", reading=2.0))
    await gom.save(Held(id="w2", reading=2.0, at=datetime(2024, 3, 15, 10, 30, tzinfo=UTC)))
    alone, beside = await stored(pm, "w1"), await stored(pm, "w2")
    assert alone["reading"] == beside["reading"]
    for id in ("w1", "w2"):
        loaded = await gom.load(Held, id)
        assert loaded is not None and loaded.reading == 2.0
        # On Oracle a saved float reads back as a number until #830 (see ORACLE_FLOATS, held on its own test).
        assert isinstance(loaded.reading, float) or pm.type is DatabaseType.ORACLE


ORACLE_FLOATS = (
    "Oracle's saves write a float as a JSON number, which Oracle holds as a number, so a saved 2.0 reads back as 2, "
    "until they take the binary-double form of liberation-data/drivine4j#830 (store layout 2), with Drivine for "
    "TypeScript and the goldens the two share moving together, before 0.9.0 ships. A port to drivine-python is filed "
    "once #830's approach settles."
)
"""The hold on a saved float on Oracle: see value_layout.ORACLE_SAVES_AS."""


async def test_a_saved_float_reads_back_as_a_float_by_the_object_manager_and_by_cypher(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    async def check() -> None:
        await gom.save(Held(id="w3", reading=2.0))
        loaded = await gom.load(Held, "w3")
        assert loaded is not None and isinstance(loaded.reading, float), loaded
        read = await pm.get_one(QuerySpecification[Any](cypher_statement("MATCH (n:Held {id: 'w3'}) RETURN n.reading")))
        assert isinstance(read, float) and read == 2.0, read

    if pm.type is not DatabaseType.ORACLE:
        await check()
        return
    try:
        await check()
    except AssertionError:
        pytest.xfail(ORACLE_FLOATS)
    pytest.fail(f"Held, and passing: take the hold off. {ORACLE_FLOATS}")


async def test_nan_is_refused_by_name_where_the_store_holds_it_as_no_number(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    with pytest.raises(DrivineError, match="NaN"):
        await gom.save(Held(id="n1", reading=math.nan))
    assert await gom.load(Held, "n1") is None


async def test_bytes_are_held_as_the_list_of_their_signed_values_and_read_back_as_the_bytes_they_are(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    await gom.save(Held(id="b1", raw=b"\x01\x02\xff"))
    assert (await stored(pm, "b1"))["raw"] == [1, 2, -1]
    loaded = await gom.load(Held, "b1")
    assert loaded is not None and loaded.raw == b"\x01\x02\xff"
