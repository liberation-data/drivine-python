"""
A save's values as store layout 2 holds them on each SQL store, written by the shared module and read back through the
object manager (see value_layout): a property whose name begins with `$`, the floats JSON writes no number of beside a
date, one float alike with a date beside it or without, NaN refused by name where the store holds it as no number, and
bytes the list of their signed values. host.yaml's "Values a save writes" pins what the module writes.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import Annotated, Any

import pytest

from drivine import (
    CaseInsensitive,
    DatabaseType,
    DrivineError,
    GraphProperty,
    NodeFragment,
    NodeId,
    PersistenceManager,
    QuerySpecification,
    StatelessGraphObjectManager,
    cypher_statement,
    node_ref,
    refs,
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


async def test_a_patch_and_a_patch_all_hold_their_values_as_a_save_does(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    await gom.save_all([Held(id="p1"), Held(id="p2")])
    await gom.patch(Held, "p1", set={"dollar": 7, "born": date(2024, 1, 1)})
    await gom.patch_all([Held(id="p2", dollar=8, born=date(2024, 1, 2))], only=["dollar", "born"])
    for id, dollar, born in (("p1", 7, date(2024, 1, 1)), ("p2", 8, date(2024, 1, 2))):
        held = await stored(pm, id)
        assert held["$$dollar"] == dollar and held["born"]["$t"] == "date"
        loaded = await gom.load(Held, id)
        assert loaded is not None and loaded.dollar == dollar and loaded.born == born


async def test_a_relationships_values_are_held_as_a_nodes_are(gom: StatelessGraphObjectManager, pm: PersistenceManager):
    await gom.save_all([Held(id="r1"), Held(id="r2")])
    values = {"$w": 3, "on": date(2024, 1, 1)}
    assert await gom.edges.relate(node_ref(Held, "r1"), node_ref(Held, "r2"), "HOLDS", values)
    if pm.type is DatabaseType.ORACLE:
        text = "SELECT JSON_SERIALIZE(props) AS props FROM drivine_edge WHERE type = 'HOLDS'"
    elif pm.type is DatabaseType.SQLITE:
        text = "SELECT props FROM drivine_edge WHERE type = 'HOLDS'"
    else:
        text = "SELECT props::text AS props FROM drivine_edge WHERE type = 'HOLDS'"
    [row] = await pm.query(QuerySpecification[Any](text))
    props = row["props"] if isinstance(row, dict) else row
    held: dict[str, Any] = json.loads(props) if isinstance(props, str) else props
    assert held["$$w"] == 3 and held["on"]["$t"] == "date" and "$w" not in held


class CaseHeld(NodeFragment, labels=["CaseHeld"]):
    id: Annotated[str, NodeId()]
    mail: Annotated[str | None, GraphProperty("$mail"), CaseInsensitive()] = None


async def test_a_dollar_named_case_insensitive_property_is_found_ignoring_case(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    if pm.type is not DatabaseType.SQLITE:
        pytest.skip("Measured on SQLite only so far (drivine-cypher-spec#355)")
    await gom.save(CaseHeld(id="c1", mail="A@X.com"))
    found = [it.id for it in await gom.load_all(CaseHeld, where=refs(CaseHeld).mail.eq_ignore_case("a@x.COM"))]
    if found != ["c1"]:
        # How the lowercased copy of a $-named property is named is unsettled across the libraries.
        pytest.xfail("the lowercased copy of a $-named property is named otherwise (drivine-cypher-spec#355)")
    pytest.fail("A $-named case-insensitive property is found ignoring case now: take out the hold (#355)")


async def test_a_patch_that_removes_a_dollar_named_property_removes_it_as_the_layout_holds_it(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    await gom.save(Held(id="u1", dollar=7))
    await gom.patch(Held, "u1", set={"dollar": None})
    assert "$$dollar" not in await stored(pm, "u1")
    loaded = await gom.load(Held, "u1")
    assert loaded is not None and loaded.dollar is None


class Blobs(NodeFragment, labels=["Blobs"]):
    id: Annotated[str, NodeId()]
    parts: list[bytes] | None = None


async def test_a_list_of_bytes_is_read_back_as_the_bytes_it_held(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    await gom.save(Blobs(id="l1", parts=[b"\x01\xff", b""]))
    loaded = await gom.load(Blobs, "l1")
    assert loaded is not None and loaded.parts == [b"\x01\xff", b""]


class Keyed(NodeFragment, labels=["Keyed"]):
    id: Annotated[str, NodeId()]
    named: dict[str, bytes] | None = None
    kept: tuple[bytes, ...] | None = None


async def test_a_dict_or_tuple_of_bytes_is_read_back_as_the_bytes_it_held(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    await gom.save(Keyed(id="k1", named={"a": b"\x01\xff", "b": b""}, kept=(b"\x02",)))
    assert (await stored(pm, "k1"))["named"] == {"a": [1, -1], "b": []}
    loaded = await gom.load(Keyed, "k1")
    assert loaded is not None and loaded.named == {"a": b"\x01\xff", "b": b""} and loaded.kept == (b"\x02",)


class Dated(NodeFragment, labels=["DatedHeld"]):
    id: Annotated[str, NodeId()]
    on: date | None = None
    at: datetime | None = None
    clock: time | None = None
    clocks: list[time] | None = None


DSL_TEMPORALS = (
    "the query DSL compares a saved date or moment by its text, where store layout 2 holds it as its tag, until its "
    "comparisons come from the shared module's fragments (a later PR of drivine-python#471)"
)
"""The hold on Postgres and Oracle: SQLite's DSL reads the tag's text (see sqlite_graph.temporal_text)."""


async def test_a_saved_date_or_moment_is_compared_and_ordered_by_the_query_dsl_as_it_is(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    async def check() -> None:
        base = datetime(2024, 5, 6, 7, 8, 9, 123000, tzinfo=UTC)
        await gom.save_all(
            [
                Dated(id="t1", on=date(2024, 1, 1), at=base),
                Dated(id="t2", on=date(2024, 1, 2), at=base.replace(second=10)),
            ]
        )
        q = refs(Dated)
        assert [it.id for it in await gom.load_all(Dated, q.on.eq(date(2024, 1, 2)))] == ["t2"]
        assert [it.id for it in await gom.load_all(Dated, q.at.gt(base))] == ["t2"]
        assert [it.id for it in await gom.load_all(Dated, order_by=q.on.desc())] == ["t2", "t1"]

    if pm.type is DatabaseType.SQLITE:
        await check()
        return
    try:
        await check()
    except AssertionError:
        pytest.xfail(DSL_TEMPORALS)
    pytest.fail(f"Held, and passing: take the hold off. {DSL_TEMPORALS}")


@pytest.mark.parametrize("test", ["eq", "in", "has_item"])
async def test_the_query_dsl_refuses_a_time_at_an_offset_with_seconds_by_name_as_a_parameter_is(
    gom: StatelessGraphObjectManager, test: str
):
    # The store holds an offset to the minute (#517); the DSL wrote this one as a text no value of it matches (#523).
    at = time(10, tzinfo=timezone(timedelta(hours=1, seconds=1)))
    dated = refs(Dated)
    tested = {
        "eq": lambda: dated.clock.eq(at),
        "in": lambda: dated.clock.in_([time(9), at]),
        "has_item": lambda: dated.clocks.has_item(at),
    }[test]()
    with pytest.raises(DrivineError, match="at an offset with seconds"):
        await gom.load_all(Dated, tested)
