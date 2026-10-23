"""A plain index in the way of a uniqueness constraint, as Neo4j reports it: drivine4j's ConstraintManagerTest."""

import dataclasses
import logging
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
