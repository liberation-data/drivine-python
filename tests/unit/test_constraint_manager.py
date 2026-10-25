"""A plain index in the way of a uniqueness constraint, as Neo4j reports it: drivine4j's ConstraintManagerTest."""

import dataclasses
import logging
import time
from collections.abc import Mapping
from typing import Any

import pytest
from neo4j.exceptions import Neo4jError

from drivine import DrivineError, uniqueness_constraint
from drivine.schema.constraint_manager import ConstraintManager
from drivine.schema.ensure_result import EnsureResult
from drivine.schema.native_operation import NativeOperation
from drivine.schema.schema_grammar import (
    FALKORDB_SCHEMA,
    INDEX_ALREADY_EXISTS,
    MEMGRAPH_SCHEMA,
    NEO4J_SCHEMA,
)
from drivine.schema.schema_item import SchemaItem
from drivine.schema.schema_listing import SchemaListing
from drivine.store.store_identity_resolver import StoreIdentityResolver
from drivine.sync.schema.constraint_manager import ConstraintManager as SyncConstraintManager

GRAMMAR = dataclasses.replace(
    NEO4J_SCHEMA,
    list_ranges=SchemaListing("LIST RANGES", lambda rows: rows),
    list_uniques=SchemaListing("LIST UNIQUES", lambda rows: rows),
)


def bolt(code: str) -> Neo4jError:
    return Neo4jError._hydrate_neo4j(code=code, message=code)  # pyright: ignore[reportPrivateUsage]


def refused(code: str = INDEX_ALREADY_EXISTS) -> DrivineError:
    """The engine's refusal as the executor raises it: the driver's error under Drivine's."""
    error = bolt(code)
    try:
        raise DrivineError.with_root_cause(error) from error
    except DrivineError as wrapped:
        return wrapped


class Neo4jLike:
    """An executor over a store that refuses a constraint while a plain index over its properties is there."""

    def __init__(self, *ranges: SchemaItem, repeated: list[Any] | None = None, unreadable: bool = False) -> None:
        self.ranges = list(ranges)
        self.uniques: list[SchemaItem] = []
        self.repeated = repeated or []
        self.unreadable = unreadable
        self.ran: list[str] = []
        self.refusal: BaseException = refused()

    async def run(self, statement: str | NativeOperation, bindings: Mapping[str, Any] | None = None) -> list[Any]:
        assert isinstance(statement, str)
        self.ran.append(statement)
        if statement == "LIST RANGES":
            return list(self.ranges)
        if statement == "LIST UNIQUES":
            return list(self.uniques)
        if statement.startswith("MATCH"):
            if self.unreadable:
                raise DrivineError("the store can't be read")
            return self.repeated
        if statement.startswith("CREATE CONSTRAINT"):
            if any(it.label == "Q" and set(it.properties) == {"e", "f"} for it in self.ranges):
                raise self.refusal
            self.uniques.append(SchemaItem("Q", ("e", "f"), "Q_e_f_unique"))
            return []
        if statement.startswith("DROP INDEX"):
            self.ranges = [it for it in self.ranges if statement.split()[2].strip("`") != it.name]
            return []
        raise AssertionError(statement)

    @property
    def dropped(self) -> list[str]:
        return [it for it in self.ran if it.startswith("DROP")]


SPEC = uniqueness_constraint("Q", ["e", "f"])
HAND_MADE = SchemaItem("Q", ("e", "f"), "hand_made")


def manager(store: Neo4jLike) -> ConstraintManager:
    return ConstraintManager(store, GRAMMAR)  # pyright: ignore[reportArgumentType]


async def test_an_index_in_the_way_is_reported_by_name_and_left():
    store = Neo4jLike(HAND_MADE)

    result = await manager(store).ensure(SPEC)

    assert result.status == "BLOCKED_BY_INDEX" and result.index == HAND_MADE
    assert store.ranges == [HAND_MADE] and store.dropped == [] and store.uniques == []


async def test_with_the_opt_in_the_index_is_dropped_and_the_constraint_made():
    store = Neo4jLike(HAND_MADE)

    result = await manager(store).ensure(SPEC, replace_blocking_index=True)

    assert result.status == "CREATED"
    assert store.dropped == ["DROP INDEX hand_made IF EXISTS"]
    assert store.ranges == [] and [it.properties for it in store.uniques] == [("e", "f")]


async def test_with_the_opt_in_a_repeated_key_keeps_the_index_and_is_a_violation_though_no_sample_is_asked():
    store = Neo4jLike(HAND_MADE, repeated=[["x", "y"]])

    result = await manager(store).ensure(SPEC, 0, replace_blocking_index=True)

    assert result.status == "VIOLATION" and result.sample == ()
    assert store.ranges == [HAND_MADE] and store.dropped == []


async def test_with_the_opt_in_an_unreadable_store_keeps_the_index_and_the_refusal_is_raised():
    store = Neo4jLike(HAND_MADE, unreadable=True)

    with pytest.raises(DrivineError) as raised:
        await manager(store).ensure(SPEC, 0, replace_blocking_index=True)

    assert raised.value is store.refusal
    assert GRAMMAR.blocked_by_index(raised.value)
    assert any("the data could not be read" in it for it in raised.value.__notes__)
    assert store.ranges == [HAND_MADE] and store.dropped == []


async def test_the_data_checked_before_the_constraint_is_not_read_again():
    store = Neo4jLike(HAND_MADE)

    await manager(store).ensure(SPEC, replace_blocking_index=True)

    assert len([it for it in store.ran if it.startswith("MATCH")]) == 2, "once before each attempt to make it"


async def test_the_index_over_the_properties_in_their_order_is_the_one_in_the_way():
    reversed_ = SchemaItem("Q", ("f", "e"), "reversed")
    store = Neo4jLike(reversed_, HAND_MADE)

    result = await manager(store).ensure(SPEC)

    assert result.index == HAND_MADE


async def test_with_no_such_index_the_refusal_is_raised():
    store = Neo4jLike(SchemaItem("Q", ("f", "e"), "reversed"))

    with pytest.raises(DrivineError) as raised:
        await manager(store).ensure(SPEC, replace_blocking_index=True)

    assert raised.value is store.refusal and store.dropped == []


async def test_another_refusal_is_raised_as_it_is():
    store = Neo4jLike(HAND_MADE)
    store.refusal = refused("Neo.ClientError.Schema.IndexWithNameAlreadyExists")

    with pytest.raises(DrivineError) as raised:
        await manager(store).ensure(SPEC, replace_blocking_index=True)

    assert raised.value is store.refusal and store.dropped == []


class FalkorDbLike:
    """
    An executor over a FalkorDB, which builds a constraint in the background: once made, the constraint is missing
    from the first `pending` listings, then listed UNDER CONSTRUCTION for `building` more, and then listed with
    `status` until it is dropped. It answers the query for keys
    held more than once with `conflicts` in turn, the last one from then on, or raises it when that is an error.
    """

    def __init__(self, status: str, pending: int = 1, *conflicts: list[Any] | Exception, building: int = 0) -> None:
        self.status = status
        self.pending = pending
        self.building = building
        self.conflicts = conflicts or ([],)
        self.made = False
        self.listings = 0
        self.dropped = False
        self.read: list[str] = []

    async def run(self, statement: str | NativeOperation, bindings: Mapping[str, Any] | None = None) -> list[Any]:
        return self.answer(statement)

    def answer(self, statement: str | NativeOperation) -> list[Any]:
        if isinstance(statement, NativeOperation):
            self.made = statement.native == "CONSTRAINT_CREATE"
            self.dropped = self.dropped or statement.native == "CONSTRAINT_DROP"
            return []
        if statement == FALKORDB_SCHEMA.list_uniques.statement and self.made:
            self.listings += 1
            if self.listings <= self.pending:
                return []
            status = "UNDER CONSTRUCTION" if self.listings <= self.pending + self.building else self.status
            return [{"label": "Person", "properties": ["email"], "status": status}]
        if "holders > 1" in statement:
            answer = self.conflicts[min(len(self.read), len(self.conflicts) - 1)]
            self.read.append(statement)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return []


class SyncFalkorDbLike(FalkorDbLike):
    """The same FalkorDB, for the sync API's executor."""

    def run(self, statement: str | NativeOperation, bindings: Mapping[str, Any] | None = None) -> list[Any]:  # pyright: ignore[reportIncompatibleMethodOverride]
        return self.answer(statement)


PERSON_EMAIL = uniqueness_constraint("Person", "email")
REPEATED = {"properties": {"email": "dup@example.com"}, "count": 2, "nodes": [{"elementId": 1}, {"elementId": 2}]}
OTHER = {"properties": {"email": "other@example.com"}, "count": 2, "nodes": [{"elementId": 3}, {"elementId": 4}]}


async def test_on_falkordb_a_constraint_built_in_the_background_is_waited_for():
    store = FalkorDbLike("OPERATIONAL", 2)

    result = await ConstraintManager(store, FALKORDB_SCHEMA).ensure(PERSON_EMAIL, 0)  # pyright: ignore[reportArgumentType]

    assert result == EnsureResult("CREATED", PERSON_EMAIL)


async def test_on_falkordb_a_constraint_listed_under_construction_is_not_taken_for_built_as_its_build_may_fail():
    store = FalkorDbLike("FAILED", 1, [REPEATED], building=2)

    result = await ConstraintManager(store, FALKORDB_SCHEMA).ensure(PERSON_EMAIL, 0)  # pyright: ignore[reportArgumentType]

    assert result == EnsureResult("VIOLATION", PERSON_EMAIL, sample=())
    assert store.dropped


async def test_on_falkordb_an_attempt_reads_the_statuses_from_one_listing_so_a_build_failing_between_is_not_built():
    # UNDER CONSTRUCTION in the first two listings, FAILED from the third.
    store = FalkorDbLike("FAILED", 0, [REPEATED], building=2)

    result = await ConstraintManager(store, FALKORDB_SCHEMA).ensure(PERSON_EMAIL, 0)  # pyright: ignore[reportArgumentType]

    assert result == EnsureResult("VIOLATION", PERSON_EMAIL, sample=())
    # One listing per attempt: two UNDER CONSTRUCTION, one FAILED.
    assert store.listings == 3


async def test_on_falkordb_a_failed_build_is_dropped_and_a_violation_with_an_empty_sample_at_sample_size_0():
    store = FalkorDbLike("FAILED", 1, [REPEATED])
    started = time.monotonic()

    result = await ConstraintManager(store, FALKORDB_SCHEMA).ensure(PERSON_EMAIL, 0)  # pyright: ignore[reportArgumentType]

    assert result == EnsureResult("VIOLATION", PERSON_EMAIL, sample=())
    assert time.monotonic() - started < 1
    assert store.dropped
    # The data is read only after the build failed, to confirm the violation, for one key.
    assert store.read == [FALKORDB_SCHEMA.conflicts(PERSON_EMAIL, 1)]


async def test_on_falkordb_a_build_that_fails_after_the_data_was_read_gives_its_keys_up_to_the_sample_size():
    # Clean when ensure reads it first; two keys held more than once by the time the build fails.
    store = FalkorDbLike("FAILED", 1, [], [REPEATED, OTHER])

    result = await ConstraintManager(store, FALKORDB_SCHEMA).ensure(PERSON_EMAIL, 2)  # pyright: ignore[reportArgumentType]

    assert result == EnsureResult("VIOLATION", PERSON_EMAIL, sample=(REPEATED, OTHER))
    assert store.dropped
    assert store.read == [FALKORDB_SCHEMA.conflicts(PERSON_EMAIL, 2)] * 2


async def test_on_falkordb_a_failed_build_with_no_key_held_twice_is_dropped_and_raised():
    store = FalkorDbLike("FAILED", 1, [])

    with pytest.raises(DrivineError) as raised:
        await ConstraintManager(store, FALKORDB_SCHEMA).ensure(PERSON_EMAIL, 0)  # pyright: ignore[reportArgumentType]

    assert str(raised.value) == (
        "FalkorDB could not build the uniqueness constraint on Person(email), "
        "but no key it covers is held more than once."
    )
    assert store.dropped


async def test_on_falkordb_a_failed_build_whose_data_cannot_be_read_is_dropped_and_raised_keeping_why():
    unreadable = ConnectionError("Connection lost")
    store = FalkorDbLike("FAILED", 1, unreadable)

    with pytest.raises(DrivineError) as raised:
        await ConstraintManager(store, FALKORDB_SCHEMA).ensure(PERSON_EMAIL, 0)  # pyright: ignore[reportArgumentType]

    assert str(raised.value) == (
        "FalkorDB could not build the uniqueness constraint on Person(email), "
        "and the data could not be read to see why."
    )
    assert raised.value.__cause__ is unreadable
    assert store.dropped


async def test_on_falkordb_drop_removes_a_constraint_whose_build_failed_which_list_leaves_out():
    store = FalkorDbLike("FAILED", 0)
    store.made = True
    constraints = ConstraintManager(store, FALKORDB_SCHEMA)  # pyright: ignore[reportArgumentType]

    assert await constraints.list() == []
    await constraints.drop(PERSON_EMAIL)

    assert store.dropped


def test_on_falkordb_the_sync_api_waits_for_a_constraint_built_in_the_background():
    store = SyncFalkorDbLike("OPERATIONAL", 2)

    result = SyncConstraintManager(store, FALKORDB_SCHEMA).ensure(PERSON_EMAIL, 0)  # pyright: ignore[reportArgumentType]

    assert result == EnsureResult("CREATED", PERSON_EMAIL)


def test_on_falkordb_the_sync_api_does_not_take_a_constraint_listed_under_construction_for_built():
    store = SyncFalkorDbLike("FAILED", 1, [REPEATED], building=2)

    result = SyncConstraintManager(store, FALKORDB_SCHEMA).ensure(PERSON_EMAIL, 0)  # pyright: ignore[reportArgumentType]

    assert result == EnsureResult("VIOLATION", PERSON_EMAIL, sample=())
    assert store.dropped


def test_on_falkordb_the_sync_api_reads_an_attempts_statuses_from_one_listing():
    store = SyncFalkorDbLike("FAILED", 0, [REPEATED], building=2)

    result = SyncConstraintManager(store, FALKORDB_SCHEMA).ensure(PERSON_EMAIL, 0)  # pyright: ignore[reportArgumentType]

    assert result == EnsureResult("VIOLATION", PERSON_EMAIL, sample=())
    assert store.listings == 3


def test_on_falkordb_the_sync_api_drops_a_failed_build_and_gives_a_violation():
    store = SyncFalkorDbLike("FAILED", 1, [REPEATED])
    started = time.monotonic()

    result = SyncConstraintManager(store, FALKORDB_SCHEMA).ensure(PERSON_EMAIL, 0)  # pyright: ignore[reportArgumentType]

    assert result == EnsureResult("VIOLATION", PERSON_EMAIL, sample=())
    assert time.monotonic() - started < 1
    assert store.dropped


class BuildingInStore:
    """
    An executor over a FalkorDB holding a constraint another process is building. Until it is dropped, each listing
    gives the next of `builds`, the last from then on; None lists nothing, as when that process has dropped its failed
    build. Once dropped, nothing is listed until the constraint is made, and then each listing gives the next of
    `rebuilds`. The query for keys held more than once gives `conflicts`.
    """

    def __init__(
        self, *builds: str | None, rebuilds: tuple[str, ...] = ("OPERATIONAL",), conflicts: list[Any] | None = None
    ) -> None:
        self.builds = builds
        self.rebuilds = rebuilds
        self.conflicts = conflicts or []
        self.phase = "building"
        self.listings = 0
        self.ran: list[str | NativeOperation] = []

    async def run(self, statement: str | NativeOperation, bindings: Mapping[str, Any] | None = None) -> list[Any]:
        return self.answer(statement)

    def answer(self, statement: str | NativeOperation) -> list[Any]:
        self.ran.append(statement)
        if isinstance(statement, NativeOperation):
            if statement.native == "CONSTRAINT_DROP":
                self.phase = "dropped"
            elif statement.native == "CONSTRAINT_CREATE":
                self.phase, self.listings = "made", 0
            return []
        if statement == FALKORDB_SCHEMA.list_uniques.statement:
            statuses = {"building": self.builds, "made": self.rebuilds}.get(self.phase)
            if not statuses:
                return []
            status = statuses[min(self.listings, len(statuses) - 1)]
            self.listings += 1
            return [{"label": "Person", "properties": ["email"], "status": status}] if status else []
        return self.conflicts if "holders > 1" in statement else []

    def count(self, native: str) -> int:
        return sum(1 for it in self.ran if isinstance(it, NativeOperation) and it.native == native)

    def listed(self) -> int:
        return sum(1 for it in self.ran if it == FALKORDB_SCHEMA.list_uniques.statement)


class SyncBuildingInStore(BuildingInStore):
    """The same FalkorDB, for the sync API's executor."""

    def run(self, statement: str | NativeOperation, bindings: Mapping[str, Any] | None = None) -> list[Any]:  # pyright: ignore[reportIncompatibleMethodOverride]
        return self.answer(statement)


async def test_on_falkordb_a_constraint_already_under_construction_is_waited_on_and_matching_once_built():
    store = BuildingInStore("UNDER CONSTRUCTION", "UNDER CONSTRUCTION", "OPERATIONAL")

    result = await ConstraintManager(store, FALKORDB_SCHEMA).ensure(PERSON_EMAIL)  # pyright: ignore[reportArgumentType]

    assert result == EnsureResult("ALREADY_MATCHING", PERSON_EMAIL)
    assert store.listed() == 3
    assert (store.count("CONSTRAINT_DROP"), store.count("CONSTRAINT_CREATE")) == (0, 0)


async def test_on_falkordb_a_constraint_already_under_construction_whose_build_fails_is_dropped_and_a_violation():
    store = BuildingInStore("UNDER CONSTRUCTION", "FAILED", rebuilds=("FAILED",), conflicts=[REPEATED])

    result = await ConstraintManager(store, FALKORDB_SCHEMA).ensure(PERSON_EMAIL)  # pyright: ignore[reportArgumentType]

    assert result == EnsureResult("VIOLATION", PERSON_EMAIL, sample=(REPEATED,))
    # Dropped, and not made again over data that holds a key more than once.
    assert (store.count("CONSTRAINT_DROP"), store.count("CONSTRAINT_CREATE")) == (1, 0)


async def test_on_falkordb_a_constraint_already_under_construction_whose_build_fails_is_made_again_on_clean_data():
    store = BuildingInStore("UNDER CONSTRUCTION", "FAILED")

    result = await ConstraintManager(store, FALKORDB_SCHEMA).ensure(PERSON_EMAIL)  # pyright: ignore[reportArgumentType]

    assert result == EnsureResult("CREATED", PERSON_EMAIL)
    natives = [it.native for it in store.ran if isinstance(it, NativeOperation)]
    assert natives == ["CONSTRAINT_DROP", "CONSTRAINT_CREATE"]


async def test_on_falkordb_a_constraint_already_failed_is_dropped_and_a_violation_at_sample_size_0_when_rebuilt():
    store = BuildingInStore("FAILED", rebuilds=("FAILED",), conflicts=[REPEATED])

    result = await ConstraintManager(store, FALKORDB_SCHEMA).ensure(PERSON_EMAIL, 0)  # pyright: ignore[reportArgumentType]

    assert result == EnsureResult("VIOLATION", PERSON_EMAIL, sample=())
    # The one already there, then the one made again, which failed too.
    assert store.count("CONSTRAINT_DROP") == 2


async def test_on_falkordb_a_constraint_dropped_while_under_construction_is_made():
    store = BuildingInStore("UNDER CONSTRUCTION", None)

    result = await ConstraintManager(store, FALKORDB_SCHEMA).ensure(PERSON_EMAIL)  # pyright: ignore[reportArgumentType]

    assert result == EnsureResult("CREATED", PERSON_EMAIL)
    assert (store.count("CONSTRAINT_DROP"), store.count("CONSTRAINT_CREATE")) == (0, 1)


def test_on_falkordb_the_sync_api_waits_on_a_constraint_already_under_construction():
    built = SyncBuildingInStore("UNDER CONSTRUCTION", "OPERATIONAL")
    failed = SyncBuildingInStore("UNDER CONSTRUCTION", "FAILED", rebuilds=("FAILED",), conflicts=[REPEATED])

    assert SyncConstraintManager(built, FALKORDB_SCHEMA).ensure(PERSON_EMAIL) == EnsureResult(  # pyright: ignore[reportArgumentType]
        "ALREADY_MATCHING", PERSON_EMAIL
    )
    assert SyncConstraintManager(failed, FALKORDB_SCHEMA).ensure(PERSON_EMAIL) == EnsureResult(  # pyright: ignore[reportArgumentType]
        "VIOLATION", PERSON_EMAIL, sample=(REPEATED,)
    )
    assert failed.count("CONSTRAINT_DROP") == 1


def test_only_falkordb_lists_the_constraints_it_failed_to_build():
    assert FALKORDB_SCHEMA.list_failed_uniques is not None
    assert FALKORDB_SCHEMA.list_failed_uniques.statement == FALKORDB_SCHEMA.list_uniques.statement
    rows = [
        {"label": "Person", "properties": ["email"], "status": "FAILED"},
        {"label": "Person", "properties": ["name"], "status": "OPERATIONAL"},
        {"label": "Person", "properties": ["uuid"], "status": "UNDER CONSTRUCTION"},
    ]
    assert FALKORDB_SCHEMA.list_failed_uniques.read(rows) == [SchemaItem("Person", ("email",))]
    assert [it.properties for it in FALKORDB_SCHEMA.list_uniques.read(rows)] == [("name",), ("uuid",)]
    assert FALKORDB_SCHEMA.list_building_uniques is not None
    assert FALKORDB_SCHEMA.list_building_uniques.read(rows) == [SchemaItem("Person", ("uuid",))]
    assert NEO4J_SCHEMA.list_failed_uniques is None and MEMGRAPH_SCHEMA.list_failed_uniques is None
    assert NEO4J_SCHEMA.list_building_uniques is None and MEMGRAPH_SCHEMA.list_building_uniques is None


def test_neo4j_knows_an_index_in_the_way_by_its_code_not_by_a_name_clash_or_by_prose():
    assert NEO4J_SCHEMA.blocked_by_index(bolt(INDEX_ALREADY_EXISTS))
    assert NEO4J_SCHEMA.blocked_by_index(refused())
    assert not NEO4J_SCHEMA.blocked_by_index(bolt("Neo.ClientError.Schema.IndexWithNameAlreadyExists"))
    assert not NEO4J_SCHEMA.blocked_by_index(
        DrivineError(f"There already exists an index (:Q {{e}}). {INDEX_ALREADY_EXISTS}")
    )


def test_no_other_engine_reports_an_index_in_the_way():
    for grammar in (MEMGRAPH_SCHEMA, FALKORDB_SCHEMA):
        assert not grammar.blocked_by_index(bolt(INDEX_ALREADY_EXISTS))
        assert grammar.drop_named_range("hand_made") == []


async def test_an_index_the_engine_does_not_name_is_not_dropped_and_the_refusal_is_raised():
    store = Neo4jLike(SchemaItem("Q", ("e", "f")))

    with pytest.raises(DrivineError) as raised:
        await manager(store).ensure(SPEC, replace_blocking_index=True)

    assert raised.value is store.refusal and store.dropped == []


async def test_a_constraint_refused_once_the_index_is_dropped_raises_and_leaves_neither():
    class RefusedAgain(Neo4jLike):
        async def run(self, statement: str | NativeOperation, bindings: Mapping[str, Any] | None = None) -> list[Any]:
            if isinstance(statement, str) and statement.startswith("CREATE CONSTRAINT") and not self.ranges:
                raise DrivineError("refused again")
            return await super().run(statement, bindings)

    store = RefusedAgain(HAND_MADE)

    with pytest.raises(DrivineError, match="refused again"):
        await manager(store).ensure(SPEC, replace_blocking_index=True)

    assert store.dropped == ["DROP INDEX hand_made IF EXISTS"]
    assert store.ranges == [] and store.uniques == [], "as drivine4j leaves it: the index is not made again"


async def test_the_store_key_with_an_index_in_the_way_is_not_in_force_and_warned_of(caplog):
    class Blocked:
        async def ensure(self, spec: Any, sample_size: int = 10, replace_blocking_index: bool = False) -> Any:
            return EnsureResult("BLOCKED_BY_INDEX", spec, index=HAND_MADE)

    class Pm:
        database = "neo4j"

    resolver = StoreIdentityResolver(Pm(), None, Blocked())  # pyright: ignore[reportArgumentType]

    with caplog.at_level(logging.WARNING, "drivine.store"):
        guarded = await resolver._ensure_unique_key()  # pyright: ignore[reportPrivateUsage]

    assert guarded is False, "as drivine4j's StoreIdentityResolver: best-effort until the index is dropped"
    assert "hand_made" in caplog.text and "in the way" in caplog.text
