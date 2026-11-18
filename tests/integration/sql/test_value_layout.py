"""
A save's values as store layout 2 holds them on each SQL store, written by the shared module and read back through the
object manager (see value_layout): a property whose name begins with `$`, the floats JSON writes no number of beside a
date, one float alike with a date beside it or without, NaN refused by name where the store holds it as no number, and
bytes the list of their signed values. host.yaml's "Values a save writes" pins what the module writes.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import Annotated, Any
from zoneinfo import ZoneInfo

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
    above,
    below,
    case_of,
    count,
    cypher_statement,
    equal_to,
    key,
    max_,
    node_ref,
    not_equal_to,
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
    if pm.type is DatabaseType.POSTGRES:
        pytest.skip("Postgres's jsonb holds no infinity")
    await gom.save(Held(id="f1", born=date(2024, 1, 1), reading=-0.0, others=[math.inf, -math.inf, 1.5]))
    held = await stored(pm, "f1")
    assert held["born"]["$t"] == "date"
    loaded = await gom.load(Held, "f1")
    assert loaded is not None and loaded.born == date(2024, 1, 1)
    assert loaded.reading == 0
    if pm.type is not DatabaseType.ORACLE:
        # Oracle may drop -0's sign, as its own CREATE does: Drivine for TypeScript's saveValues test pins it so.
        assert math.copysign(1, loaded.reading) < 0
    assert loaded.others == [math.inf, -math.inf, 1.5]


async def test_minus_zero_is_written_as_a_cypher_create_given_it_writes_it(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    # Drivine for TypeScript's saveValues "writes -0 as a CREATE given it writes it": the sign may go, as on Oracle.
    await pm.execute(QuerySpecification[Any](cypher_statement("MATCH (n:Held) WHERE n.id IN ['z', 'c'] DELETE n")))
    await gom.save(Held(id="z", reading=-0.0))
    create = cypher_statement("CREATE (n:Held {id: $id, reading: $reading})")
    await pm.execute(QuerySpecification[Any](create).bind({"id": "c", "reading": -0.0}))
    read = cypher_statement("MATCH (n:Held) WHERE n.id IN ['z', 'c'] RETURN toString(n.reading) ORDER BY n.id DESC")
    saved, made = map(str, await pm.query(QuerySpecification[Any](read)))
    assert saved == made
    assert re.fullmatch(r"-?0\.0", saved), saved


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
        assert isinstance(loaded.reading, float)


async def test_a_saved_float_reads_back_as_a_float_by_the_object_manager_and_by_cypher(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    # On Oracle a float is held as a binary double (extended JSON's, read EXTENDED), so a whole one stays a float.
    await gom.save(Held(id="w3", reading=2.0))
    loaded = await gom.load(Held, "w3")
    assert loaded is not None and isinstance(loaded.reading, float), loaded
    read = await pm.get_one(QuerySpecification[Any](cypher_statement("MATCH (n:Held {id: 'w3'}) RETURN n.reading")))
    assert isinstance(read, float) and read == 2.0, read


async def test_nan_is_refused_by_name_where_the_store_holds_it_as_no_number(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    if pm.type is DatabaseType.ORACLE:
        pytest.skip("Oracle holds NaN as a binary double")
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
    # The query DSL's filter on SQLite is the shared module's fragment, which reads the property as the layout holds
    # it, `$$mail` (drivine-cypher-spec#235).
    found = [it.id for it in await gom.load_all(CaseHeld, where=refs(CaseHeld).mail.eq_ignore_case("a@x.COM"))]
    assert found == ["c1"]


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


def refused(pm: PersistenceManager) -> str:
    """
    The refusal of a comparison with a date, datetime or time, or an order by one, by the query DSL on Oracle, which
    compares one by its text until the DSL takes the shared module's fragments there (drivine-cypher-spec#235, step 4).
    """
    return r"aren't supported on Oracle yet .*drivine-cypher-spec#235"


async def on_fragments_or_refused(pm: PersistenceManager, check: Callable[[], Awaitable[None]]) -> None:
    """
    Runs the check: on SQLite and Postgres, whose DSL is the shared module's fragments (drivine-cypher-spec#235, steps 2
    and 3), its rows must be as it asserts; on Oracle its first comparison or order by a date, datetime or time must be
    refused by name (see refused).
    """
    if pm.type is not DatabaseType.ORACLE:
        await check()
        return
    with pytest.raises(DrivineError, match=refused(pm)):
        await check()


async def test_a_saved_date_or_moment_is_compared_and_ordered_by_the_query_dsl_as_it_is_or_refused_by_name(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    """
    SQLite's and Postgres's DSL reads the tag store layout 2 holds a date or moment as, through the shared module's
    fragments (drivine-cypher-spec#235). Oracle's would compare it by its text until the DSL takes them there too, so it
    refuses it by name, and Cypher compares it there.
    """
    base = datetime(2024, 5, 6, 7, 8, 9, 123000, tzinfo=UTC)
    await gom.save_all(
        [
            Dated(id="t1", on=date(2024, 1, 1), at=base),
            Dated(id="t2", on=date(2024, 1, 2), at=base.replace(second=10)),
        ]
    )
    found = await pm.query(
        QuerySpecification[Any](cypher_statement("MATCH (n:DatedHeld) WHERE n.at > $at RETURN n.id AS id")).bind(
            {"at": base}
        )
    )
    assert [it["id"] if isinstance(it, dict) else it for it in found] == ["t2"]
    q = refs(Dated)
    if pm.type is not DatabaseType.ORACLE:
        assert [it.id for it in await gom.load_all(Dated, q.on.eq(date(2024, 1, 2)))] == ["t2"]
        assert [it.id for it in await gom.load_all(Dated, q.at.gt(base))] == ["t2"]
        # Its own nodes alone: the tests beside it save other Dated nodes in the same graph.
        assert [it.id for it in await gom.load_all(Dated, q.id.in_(["t1", "t2"]), order_by=q.on.desc())] == ["t2", "t1"]
        return
    with pytest.raises(DrivineError, match=refused(pm)) as raised:
        await gom.load_all(Dated, q.on.eq(date(2024, 1, 2)))
    assert "on is compared with" in str(raised.value)
    with pytest.raises(DrivineError, match=refused(pm)):
        await gom.load_all(Dated, q.at.gt(base))
    with pytest.raises(DrivineError, match=refused(pm)):
        await gom.load_all(Dated, q.id.in_(["t1", "t2"]), order_by=q.on.desc())


class Met(NodeFragment, labels=["MetHeld"]):
    """A moment of its own label, so its test groups only its own."""

    id: Annotated[str, NodeId()]
    name: str | None = None
    at: datetime | None = None


async def test_a_moment_in_a_patchs_where_and_in_a_grouped_reads_where_and_aggregate_is_compared_by_its_tag(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    first, second = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)
    await gom.save_all([Met(id="d1", name="one", at=first), Met(id="d2", name="two", at=second)])
    q = refs(Met)

    async def check() -> None:
        assert not (await gom.patch(Met, "d1", set={"name": "missed"}, where=q.at.eq(second))).applied
        assert (await gom.patch(Met, "d1", set={"name": "patched"}, where=q.at.eq(first))).applied
        loaded = await gom.load(Met, "d1")
        assert loaded is not None and loaded.name == "patched"
        groups = await gom.groups.load(
            Met,
            where=q.at.gte(first),
            keys={"name": key(q.name)},
            aggregates={"later": count(q.at.gt(first)), "last": max_(q.at)},
            order_by=[("name", "asc")],
        )
        # The greatest moment is read back as the type its field declares (drivine-python#537).
        assert groups == [{"name": "patched", "later": 0, "last": first}, {"name": "two", "later": 1, "last": second}]
        assert await gom.groups.load(Met, where=q.at.gt(first), keys={"name": key(q.name)}) == [{"name": "two"}]

    await on_fragments_or_refused(pm, check)


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
        assert await found(q.moments.has_item(moment)) == ["l1"]
        # The same instant at another offset is another value, as Neo4j has it: the spec's "Zoned values at one
        # instant", where `b IN [a]` is false (drivine-cypher-spec#161).
        assert await found(q.moments.has_item(moment.astimezone(timezone(timedelta(hours=-5))))) == []
        assert await found(q.clocks.has_item(clock)) == ["l1"]
        assert await found(q.clocks.has_item(time(11))) == ["l1"]
        assert await found(q.clocks.has_item(time(12))) == []
        assert await found(q.words.has_item("b")) == ["l2"]

    await on_fragments_or_refused(pm, check)


async def test_has_item_passes_over_a_text_a_list_holds_where_a_moment_is_asked_for_as_cypher_does(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    async def check() -> None:
        # Written by the user's own Cypher, so each item is the text it was written as, and not a save's tag: a text,
        # which Cypher never finds equal to a moment, though it names one.
        statement = (
            "MERGE (a:ListedHeld {id: 'l3'}) SET a.moments = ['2024-05-06T09:08:09.123456+02:00'] "
            "MERGE (b:ListedHeld {id: 'l4'}) SET b.moments = ['soon', '2024-05-06T07:08:10Z']"
        )
        await pm.execute(QuerySpecification[Any](cypher_statement(statement)))
        moment = datetime(2024, 5, 6, 7, 8, 9, 123456, tzinfo=UTC)
        assert [it.id for it in await gom.load_all(Listed, refs(Listed).moments.has_item(moment))] == []

    await on_fragments_or_refused(pm, check)


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

    await on_fragments_or_refused(pm, check)


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

    await on_fragments_or_refused(pm, check)


async def test_a_local_time_and_a_time_with_an_offset_are_unequal_and_neither_before_the_other(
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
        # Unequal, as two values of two kinds are in Cypher, though neither is before the other.
        assert await found(q.clock.neq(time(8))) == ["zoned"]
        assert await found(q.clock.neq(time(8, tzinfo=UTC))) == ["local"]
        # A time with an offset orders before a local one, as Neo4j orders them.
        assert [it.id for it in await gom.load_all(TwoKinds, order_by=q.clock.asc())] == ["zoned", "local"]
        # A seek is a comparison with the cursor, which a local time doesn't meet with a time with an offset, being
        # neither before nor after it, as a graph engine seeks.
        after = q.clock.after(time(8, tzinfo=UTC))
        assert [it.id for it in await gom.load_all(TwoKinds, order_by=q.clock.asc(), seek=after)] == []

    await on_fragments_or_refused(pm, check)


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

    await on_fragments_or_refused(pm, check)


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

    await on_fragments_or_refused(pm, check)


async def test_a_date_is_never_a_moment_nor_a_local_moment_a_moment(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    async def check() -> None:
        moment = datetime(2024, 5, 6, 7, 8, 9, tzinfo=UTC)
        await gom.save_all(
            [
                Dated(id="kinds1", on=date(2024, 5, 6), at=moment),
                Listed(id="l6", days=[date(2024, 5, 6)], moments=[moment]),
            ]
        )
        q, listed = refs(Dated), refs(Listed)

        async def dated(condition: Any) -> list[str]:
            return [it.id for it in await gom.load_all(Dated, condition)]

        async def lists(condition: Any) -> list[str]:
            return [it.id for it in await gom.load_all(Listed, condition)]

        assert await dated(q.on.eq(date(2024, 5, 6))) == ["kinds1"]
        assert await dated(q.at.eq(moment)) == ["kinds1"]
        assert await dated(q.at.eq(date(2024, 5, 6))) == []
        assert await dated(q.at.in_([date(2024, 5, 6)])) == []
        assert await dated(q.on.eq(datetime(2024, 5, 6, tzinfo=UTC))) == []
        assert await dated(q.at.eq(moment.replace(tzinfo=None))) == []
        # Yet unequal, as in Cypher (Dated's label is shared with another test, so only kinds1 is asked of).
        assert await dated([q.id.eq("kinds1"), q.at.neq(date(2024, 5, 6))]) == ["kinds1"]
        assert await dated([q.id.eq("kinds1"), q.on.neq(moment)]) == ["kinds1"]
        assert await dated(q.at.in_([date(2024, 5, 6), moment])) == ["kinds1"]
        assert await dated(q.at.in_([date(2024, 5, 6), None, time(7, 8, 9, tzinfo=UTC)])) == []
        assert await lists(listed.moments.has_item(moment)) == ["l6"]
        assert await lists(listed.moments.has_item(date(2024, 5, 6))) == []
        assert await lists(listed.days.has_item(datetime(2024, 5, 6, tzinfo=UTC))) == []
        assert await lists(listed.moments.has_item(moment.replace(tzinfo=None))) == []

    await on_fragments_or_refused(pm, check)


class Wall(NodeFragment, labels=["WallHeld"]):
    """A moment of its own label, so its test finds only its own."""

    id: Annotated[str, NodeId()]
    at: datetime | None = None
    moments: list[datetime] | None = None


async def test_a_saved_local_moment_is_compared_with_a_local_moment_as_the_moment_it_is(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    async def check() -> None:
        # A naive moment is saved a local moment, and found only by another (see _tag_of).
        local = datetime(2024, 5, 6, 7, 8, 9, 500000)
        await gom.save_all([Wall(id="w", at=local, moments=[local])])
        q = refs(Wall)

        async def found(condition: Any) -> list[str]:
            return [it.id for it in await gom.load_all(Wall, condition)]

        assert await found(q.at.eq(local)) == ["w"]
        assert await found(q.at.gt(local - timedelta(microseconds=1))) == ["w"]
        assert await found(q.at.lt(local)) == []
        assert await found(q.at.in_([local, local.replace(tzinfo=UTC)])) == ["w"]
        assert await found(q.moments.has_item(local)) == ["w"]
        assert await found(q.at.eq(local.replace(tzinfo=UTC))) == []
        assert await found(q.at.lt(local.replace(tzinfo=offset(-1)))) == []
        assert await found(q.at.neq(local.replace(tzinfo=UTC))) == ["w"]
        assert await found(q.moments.has_item(local.replace(tzinfo=UTC))) == []

    await on_fragments_or_refused(pm, check)


class Ranked(NodeFragment, labels=["RankedHeld"]):
    """Dates, moments and times of their own label, so an order by an expression orders only its own."""

    id: Annotated[str, NodeId()]
    on: date | None = None
    at: datetime | None = None
    clock: time | None = None


async def test_an_order_by_an_expression_compares_a_date_moment_or_time_as_a_condition_does(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    async def check() -> None:
        await gom.save_all(
            [
                Ranked(id="r1", on=date(2024, 3, 1), at=datetime(2024, 5, 6, 8, tzinfo=UTC)),
                # 07:00 and 09:30 in UTC.
                Ranked(
                    id="r2",
                    on=date(2024, 1, 1),
                    at=datetime(2024, 5, 6, 9, tzinfo=offset(2)),
                    clock=time(4, 30, tzinfo=offset(-5)),
                ),
                # 08:00 in UTC.
                Ranked(id="r3", clock=time(9, tzinfo=offset(1))),
                Ranked(id="r4", clock=time(8)),
            ]
        )
        q = refs(Ranked)

        async def ranked(branch: Any) -> list[str]:
            """The ids, those the branch ranks 0 first, then the rest, each by id."""
            ranking = case_of([branch.then(0)], 1)
            return [it.id for it in await gom.load_all(Ranked, order_by=[ranking.asc(), q.id.asc()])]

        assert await ranked(below(q.on, date(2024, 2, 1))) == ["r2", "r1", "r3", "r4"]
        assert await ranked(below(q.at, datetime(2024, 5, 6, 7, 30, tzinfo=UTC))) == ["r2", "r1", "r3", "r4"]
        assert await ranked(equal_to(q.at, datetime(2024, 5, 6, 9, tzinfo=offset(2)))) == ["r2", "r1", "r3", "r4"]
        # Only 09:30 UTC is after 08:15 UTC, and a local time is neither before nor after a time with an offset.
        assert await ranked(above(q.clock, time(8, 15, tzinfo=UTC))) == ["r2", "r1", "r3", "r4"]
        assert await ranked(above(q.clock, time(7))) == ["r4", "r1", "r2", "r3"]
        # A date is no moment, nor a moment a date.
        assert await ranked(equal_to(q.at, date(2024, 5, 6))) == ["r1", "r2", "r3", "r4"]
        assert await ranked(below(q.on, datetime(2025, 1, 1, tzinfo=UTC))) == ["r1", "r2", "r3", "r4"]
        # Yet unequal, as in Cypher; a missing value is neither.
        assert await ranked(not_equal_to(q.on, datetime(2025, 1, 1, tzinfo=UTC))) == ["r1", "r2", "r3", "r4"]
        assert await ranked(not_equal_to(q.clock, time(8))) == ["r2", "r3", "r1", "r4"]

    await on_fragments_or_refused(pm, check)


class Zoned(NodeFragment, labels=["ZonedHeld"]):
    """A time of its own label, so its test finds only its own."""

    id: Annotated[str, NodeId()]
    clock: time | None = None


async def test_a_time_of_a_named_zone_is_compared_as_the_local_time_a_save_writes_of_it(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    async def check() -> None:
        # A time has no offset of a named zone, whose offset changes with the date: a save writes it a local time.
        paris = time(8, tzinfo=ZoneInfo("Europe/Paris"))
        await gom.save_all([Zoned(id="z", clock=paris)])
        q = refs(Zoned)
        assert [it.id for it in await gom.load_all(Zoned, q.clock.eq(paris))] == ["z"]
        assert [it.id for it in await gom.load_all(Zoned, q.clock.eq(time(8)))] == ["z"]
        assert [it.id for it in await gom.load_all(Zoned, q.clock.eq(time(8, tzinfo=UTC)))] == []

    await on_fragments_or_refused(pm, check)


@pytest.mark.parametrize("test", ["eq", "in", "has_item"])
async def test_the_query_dsl_refuses_a_time_at_an_offset_with_seconds_by_name_as_a_parameter_is(
    gom: StatelessGraphObjectManager, pm: PersistenceManager, test: str
):
    """
    The store holds an offset to the minute (#517); the DSL wrote this one as a text no value of it matches (#523).
    Oracle refuses any comparison with a time first, by the name of drivine-cypher-spec#235.
    """
    at = time(10, tzinfo=timezone(timedelta(hours=1, seconds=1)))
    dated = refs(Dated)
    tested = {
        "eq": lambda: dated.clock.eq(at),
        "in": lambda: dated.clock.in_([time(9), at]),
        "has_item": lambda: dated.clocks.has_item(at),
    }[test]()
    with pytest.raises(
        DrivineError, match=refused(pm) if pm.type is DatabaseType.ORACLE else "at an offset with seconds"
    ):
        await gom.load_all(Dated, tested)


async def test_the_query_dsl_finds_a_time_at_an_offset_of_whole_minutes_as_it_was_before(
    gom: StatelessGraphObjectManager, pm: PersistenceManager
):
    # Only an offset with seconds is refused (#523): one of whole minutes is found as before.
    async def check() -> None:
        at = time(10, tzinfo=timezone(timedelta(hours=1)))
        await gom.save_all([Dated(id="c1", clock=at), Dated(id="c2", clock=time(9))])
        dated = refs(Dated)
        assert [it.id for it in await gom.load_all(Dated, dated.clock.eq(at))] == ["c1"]
        assert [it.id for it in await gom.load_all(Dated, dated.clock.in_([at]))] == ["c1"]

    await on_fragments_or_refused(pm, check)
