"""
A save's values as store layout 2 holds them on each SQL store, written by the shared module and read back through the
object manager (see value_layout): a property whose name begins with `$`, the floats JSON writes no number of beside a
date, one float alike with a date beside it or without, NaN refused by name where the store holds it as no number, and
bytes the list of their signed values. host.yaml's "Values a save writes" pins what the module writes.
"""

from __future__ import annotations

import json
import math
from collections.abc import Awaitable, Callable
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


class Listed(NodeFragment, labels=["ListedHeld"]):
    id: Annotated[str, NodeId()]
    days: list[date] | None = None
    moments: list[datetime] | None = None
    clocks: list[time] | None = None
    words: list[str] | None = None


async def test_the_query_dsl_finds_a_saved_date_moment_or_time_in_a_list_by_has_item(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    async def check() -> None:
        moment = datetime(2024, 5, 6, 7, 8, 9, 123000, tzinfo=UTC)
        clock = time(10, tzinfo=timezone(timedelta(hours=1)))
        await gom.save_all(
            [
                Listed(id="l1", days=[date(2024, 1, 2)], moments=[moment], clocks=[clock, time(11)], words=["a"]),
                Listed(id="l2", days=[date(2024, 1, 3)], moments=[moment.replace(second=10)], clocks=[], words=["b"]),
            ]
        )
        q = refs(Listed)

        async def found(condition: Any) -> list[str]:
            return sorted(it.id for it in await gom.load_all(Listed, condition))

        assert await found(q.days.has_item(date(2024, 1, 2))) == ["l1"]
        # The same moment at another offset is the moment it is (#525).
        assert await found(q.moments.has_item(moment.astimezone(timezone(timedelta(hours=-5))))) == ["l1"]
        assert await found(q.clocks.has_item(clock)) == ["l1"]
        assert await found(q.clocks.has_item(time(11))) == ["l1"]
        assert await found(q.clocks.has_item(time(12))) == []
        assert await found(q.words.has_item("b")) == ["l2"]

    if pm.type is DatabaseType.SQLITE:
        await check()
        return
    try:
        await check()
    except AssertionError:
        pytest.xfail(DSL_TEMPORALS)
    pytest.fail(f"Held, and passing: take the hold off. {DSL_TEMPORALS}")


async def test_has_item_finds_a_moment_a_list_holds_as_text_and_passes_over_a_text_that_names_none(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    async def check() -> None:
        # Written by the user's own Cypher, so each item is the text it was written as, and not a save's tag.
        statement = (
            "MERGE (a:ListedHeld {id: 'l3'}) SET a.moments = ['2024-05-06T09:08:09.123456+02:00'] "
            "MERGE (b:ListedHeld {id: 'l4'}) SET b.moments = ['soon', '2024-05-06T07:08:10Z']"
        )
        await pm.execute(QuerySpecification[Any](cypher_statement(statement)))
        moment = datetime(2024, 5, 6, 7, 8, 9, 123456, tzinfo=UTC)
        assert [it.id for it in await gom.load_all(Listed, refs(Listed).moments.has_item(moment))] == ["l3"]

    if pm.type is DatabaseType.SQLITE:
        await check()
        return
    try:
        await check()
    except AssertionError:
        pytest.xfail(DSL_TEMPORALS)
    pytest.fail(f"Held, and passing: take the hold off. {DSL_TEMPORALS}")


class Clocked(NodeFragment, labels=["ClockedHeld"]):
    id: Annotated[str, NodeId()]
    clock: time | None = None


class Midnight(NodeFragment, labels=["MidnightHeld"]):
    """A time of its own label, so its test orders only its own."""

    id: Annotated[str, NodeId()]
    clock: time | None = None


class TwoKinds(NodeFragment, labels=["TwoKindsHeld"]):
    """A time of its own label, so its test orders only its own."""

    id: Annotated[str, NodeId()]
    clock: time | None = None


class Local(NodeFragment, labels=["LocalHeld"]):
    """A time of its own label, so its test orders only its own."""

    id: Annotated[str, NodeId()]
    clock: time | None = None


def offset(hours: int) -> timezone:
    return timezone(timedelta(hours=hours))


async def held_or_failing(pm: PersistenceManager, check: Callable[[], Awaitable[None]]) -> None:
    """Runs the check: on SQLite it must pass; on Postgres and Oracle it is held (DSL_TEMPORALS) until it passes."""
    if pm.type is DatabaseType.SQLITE:
        await check()
        return
    try:
        await check()
    except AssertionError:
        pytest.xfail(DSL_TEMPORALS)
    pytest.fail(f"Held, and passing: take the hold off. {DSL_TEMPORALS}")


async def test_the_query_dsl_compares_and_orders_times_at_different_offsets_as_cypher_does(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    async def check() -> None:
        # drivine-python#526: 08:00:00.5, 08:30 and 09:30 in UTC, each written at another offset.
        await gom.save_all(
            [
                Clocked(id="a", clock=time(9, 0, 0, 500000, tzinfo=offset(1))),
                Clocked(id="b", clock=time(8, 30, tzinfo=UTC)),
                Clocked(id="c", clock=time(4, 30, tzinfo=offset(-5))),
            ]
        )
        q = refs(Clocked)

        async def found(condition: Any) -> list[str]:
            return sorted(it.id for it in await gom.load_all(Clocked, condition))

        assert await found(q.clock.gt(time(8, 15, tzinfo=UTC))) == ["b", "c"]
        assert await found(q.clock.lte(time(8, 30, tzinfo=UTC))) == ["a", "b"]
        assert [it.id for it in await gom.load_all(Clocked, order_by=q.clock.asc())] == ["a", "b", "c"]
        assert [it.id for it in await gom.load_all(Clocked, order_by=q.clock.desc())] == ["c", "b", "a"]
        after = q.clock.after(time(8, 30, tzinfo=UTC))
        assert [it.id for it in await gom.load_all(Clocked, order_by=q.clock.asc(), seek=after)] == ["c"]
        # Two times are equal as Neo4j has them: the same time at the same offset, whatever text wrote it.
        assert await found(q.clock.eq(time(8, 30, tzinfo=timezone(timedelta(0))))) == ["b"]
        assert await found(q.clock.eq(time(9, 0, 0, 500000, tzinfo=offset(1)))) == ["a"]
        assert await found(q.clock.eq(time(8, 0, 0, 500000, tzinfo=UTC))) == []
        assert await found(q.clock.in_([time(8, 30, tzinfo=UTC), time(4, 30, tzinfo=offset(-5))])) == ["b", "c"]

    await held_or_failing(pm, check)


async def test_a_time_with_an_offset_is_compared_by_its_utc_time_unwrapped_at_midnight(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    async def check() -> None:
        # 01:00+02:00 is 23:00 the day before in UTC: Neo4j has it an hour before midnight, before any time of the day.
        await gom.save_all(
            [Midnight(id="early", clock=time(1, tzinfo=offset(2))), Midnight(id="late", clock=time(22, tzinfo=UTC))]
        )
        q = refs(Midnight)
        assert [it.id for it in await gom.load_all(Midnight, q.clock.lt(time(0, 30, tzinfo=UTC)))] == ["early"]
        assert [it.id for it in await gom.load_all(Midnight, order_by=q.clock.asc())] == ["early", "late"]

    await held_or_failing(pm, check)


async def test_a_local_time_and_a_time_with_an_offset_meet_no_comparison_with_each_other(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    async def check() -> None:
        await gom.save_all([TwoKinds(id="local", clock=time(8)), TwoKinds(id="zoned", clock=time(8, tzinfo=UTC))])
        q = refs(TwoKinds)

        async def found(condition: Any) -> list[str]:
            return sorted(it.id for it in await gom.load_all(TwoKinds, condition))

        assert await found(q.clock.eq(time(8))) == ["local"]
        assert await found(q.clock.eq(time(8, tzinfo=UTC))) == ["zoned"]
        assert await found(q.clock.gt(time(7))) == ["local"]
        assert await found(q.clock.gt(time(7, tzinfo=UTC))) == ["zoned"]
        assert await found(q.clock.in_([time(8)])) == ["local"]
        assert await found(q.clock.in_([time(8), time(8, tzinfo=UTC)])) == ["local", "zoned"]
        # A time with an offset orders before a local one, as Neo4j orders them.
        assert [it.id for it in await gom.load_all(TwoKinds, order_by=q.clock.asc())] == ["zoned", "local"]
        after = q.clock.after(time(8, tzinfo=UTC))
        assert [it.id for it in await gom.load_all(TwoKinds, order_by=q.clock.asc(), seek=after)] == ["local"]

    await held_or_failing(pm, check)


async def test_two_local_times_are_compared_and_ordered_as_the_times_they_are(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    async def check() -> None:
        await gom.save_all([Local(id="l", clock=time(9, 30)), Local(id="m", clock=time(10, 0, 0, 250000))])
        q = refs(Local)
        assert [it.id for it in await gom.load_all(Local, q.clock.gt(time(9, 45)))] == ["m"]
        assert [it.id for it in await gom.load_all(Local, q.clock.eq(time(9, 30)))] == ["l"]
        # Saved as the module writes its fraction, `10:00:00.250`, and found as the time it is.
        assert [it.id for it in await gom.load_all(Local, q.clock.eq(time(10, 0, 0, 250000)))] == ["m"]
        assert [it.id for it in await gom.load_all(Local, order_by=q.clock.desc())] == ["m", "l"]

    await held_or_failing(pm, check)


async def test_has_item_finds_a_time_in_a_list_as_cypher_finds_one_at_its_offset(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    async def check() -> None:
        await gom.save_all(
            [Listed(id="l5", clocks=[time(8, 30, tzinfo=UTC), time(10, 0, 0, 500000, tzinfo=offset(-5))])]
        )
        q = refs(Listed)

        async def found(condition: Any) -> list[str]:
            return sorted(it.id for it in await gom.load_all(Listed, condition))

        assert await found(q.clocks.has_item(time(8, 30, tzinfo=timezone(timedelta(0))))) == ["l5"]
        assert await found(q.clocks.has_item(time(10, 0, 0, 500000, tzinfo=offset(-5)))) == ["l5"]
        # The same instant at another offset is another time, and a local time no time with an offset.
        assert await found(q.clocks.has_item(time(13, 30, tzinfo=offset(5)))) == []
        assert await found(q.clocks.has_item(time(8, 30))) == []

    await held_or_failing(pm, check)


async def test_a_date_is_never_a_moment_nor_a_local_moment_a_moment(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    async def check() -> None:
        moment = datetime(2024, 5, 6, 7, 8, 9, tzinfo=UTC)
        await gom.save_all(
            [
                Dated(id="d1", on=date(2024, 5, 6), at=moment),
                Listed(id="l6", days=[date(2024, 5, 6)], moments=[moment]),
            ]
        )
        q, listed = refs(Dated), refs(Listed)

        async def dated(condition: Any) -> list[str]:
            return [it.id for it in await gom.load_all(Dated, condition)]

        async def lists(condition: Any) -> list[str]:
            return [it.id for it in await gom.load_all(Listed, condition)]

        assert await dated(q.on.eq(date(2024, 5, 6))) == ["d1"]
        assert await dated(q.at.eq(moment)) == ["d1"]
        assert await dated(q.at.eq(date(2024, 5, 6))) == []
        assert await dated(q.at.in_([date(2024, 5, 6)])) == []
        assert await dated(q.on.eq(datetime(2024, 5, 6, tzinfo=UTC))) == []
        assert await dated(q.at.eq(moment.replace(tzinfo=None))) == []
        assert await lists(listed.moments.has_item(moment)) == ["l6"]
        assert await lists(listed.moments.has_item(date(2024, 5, 6))) == []
        assert await lists(listed.days.has_item(datetime(2024, 5, 6, tzinfo=UTC))) == []
        assert await lists(listed.moments.has_item(moment.replace(tzinfo=None))) == []

    await held_or_failing(pm, check)
