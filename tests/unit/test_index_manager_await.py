"""
An index the engine builds in the background (Neo4j, FalkorDB) is waited for, made or found there, so that `ensure`
and `recreate` return only once it is built: drivine4j's EnsureAwaitsIndexesTest, without the engines.
"""

import dataclasses
import re
from collections.abc import Mapping
from typing import Any

import pytest

from drivine import DrivineError, full_text_index, range_index, vector_index
from drivine.connection.database_type import DatabaseType
from drivine.schema.index_manager import BUILD_TIMEOUT, IndexManager, IndexNotBuilt
from drivine.schema.native_operation import NativeOperation
from drivine.schema.schema_grammar import FALKORDB_SCHEMA, NEO4J_SCHEMA, schema_grammar_for
from drivine.schema.schema_item import SchemaItem
from drivine.schema.schema_listing import SchemaListing
from drivine.schema.schema_options import SchemaOptions
from drivine.sync.schema.index_manager import IndexManager as SyncIndexManager

GRAMMAR = dataclasses.replace(
    NEO4J_SCHEMA,
    list_ranges=SchemaListing("LIST RANGES", lambda rows: rows),
    list_vectors=SchemaListing("LIST VECTORS", lambda rows: rows),
    list_full_texts=SchemaListing("LIST FULL TEXTS", lambda rows: rows),
)


class Neo4jLike:
    """
    An executor over a Neo4j that holds the index each CREATE makes under `named` (its own name, by default), and
    whose wait for an index gives `outcome`: nothing, or an error, after which the index is listed in `state`.
    """

    def __init__(
        self,
        named: str | None = None,
        outcome: Exception | None = None,
        state: dict[str, Any] | None = None,
        *held: SchemaItem,
    ) -> None:
        self.named = named
        self.outcome = outcome
        self.state = state
        self.held = list(held)
        self.ran: list[tuple[str, Mapping[str, Any]]] = []

    async def run(self, statement: str | NativeOperation, bindings: Mapping[str, Any] | None = None) -> list[Any]:
        return self.answer(statement, bindings or {})

    def answer(self, statement: str | NativeOperation, bindings: Mapping[str, Any]) -> list[Any]:
        assert isinstance(statement, str)
        self.ran.append((statement, bindings))
        if statement.startswith("LIST"):
            return list(self.held)
        if statement.startswith("CREATE"):
            made = re.search(r"INDEX `?(\w+)`? IF NOT EXISTS FOR \(n:`?(\w+)`?\) ON[^.]*n\.`?(\w+)", statement)
            assert made is not None, statement
            name, label, prop = made.groups()
            dimensions = re.search(r"`vector.dimensions`: (\d+)", statement)
            self.held.append(
                SchemaItem(
                    label,
                    (prop,),
                    self.named or name,
                    int(dimensions.group(1)) if dimensions else None,
                    "COSINE" if dimensions else None,
                )
            )
            return []
        if statement.startswith("DROP INDEX"):
            self.held = [it for it in self.held if it.name != statement.split()[2].strip("`")]
            return []
        if statement == GRAMMAR.await_index:
            if self.outcome is not None:
                raise self.outcome
            return []
        if statement == GRAMMAR.index_state:
            return [] if self.state is None else [self.state]
        raise AssertionError(statement)

    @property
    def awaited(self) -> list[Mapping[str, Any]]:
        return [bindings for statement, bindings in self.ran if statement == GRAMMAR.await_index]


def manager(store: Neo4jLike, build_timeout: float = BUILD_TIMEOUT) -> IndexManager:
    return IndexManager(store, GRAMMAR, build_timeout=build_timeout)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    "spec",
    [range_index("P", "a"), vector_index("P", "a", 3, "COSINE"), full_text_index("P", "a")],
    ids=["range", "vector", "full-text"],
)
async def test_on_neo4j_an_index_made_is_waited_for_by_its_name_for_60_seconds_after_it_is_made(spec):
    store = Neo4jLike()

    assert (await manager(store).ensure(spec)).status == "CREATED"

    [held] = store.held
    assert store.awaited == [{"name": held.name, "timeout": 60}]
    assert BUILD_TIMEOUT == SchemaOptions().index_build_timeout == 60
    statements = [it for it, _ in store.ran]
    assert statements.index(GRAMMAR.await_index or "") > statements.index(
        next(it for it in statements if it.startswith("CREATE"))
    )


async def test_on_neo4j_an_index_held_under_another_name_is_waited_for_by_the_name_neo4j_holds_it_under():
    store = Neo4jLike("made_elsewhere")

    await manager(store).ensure(range_index("P", "a"))

    assert [it["name"] for it in store.awaited] == ["made_elsewhere"]


async def test_on_neo4j_none_listed_once_made_is_not_waited_for_as_a_constraints_own_index_covers_the_properties():
    class CoveredByAConstraint(Neo4jLike):
        def answer(self, statement: str | NativeOperation, bindings: Mapping[str, Any]) -> list[Any]:
            # IF NOT EXISTS makes none, and the constraint's own index isn't listed among the plain ones.
            return (
                []
                if isinstance(statement, str) and statement.startswith("CREATE")
                else super().answer(statement, bindings)
            )

    store = CoveredByAConstraint()

    assert (await manager(store).ensure(range_index("P", "a"))).status == "CREATED"

    assert store.awaited == []


async def test_on_neo4j_an_index_there_already_is_waited_for_too_as_another_process_may_still_be_building_it():
    store = Neo4jLike(None, None, None, SchemaItem("P", ("a",), "theirs"))

    assert (await manager(store).ensure(range_index("P", "a"))).status == "ALREADY_MATCHING"

    assert [it["name"] for it in store.awaited] == ["theirs"]


async def test_on_neo4j_an_index_of_another_shape_is_drift_and_not_waited_for():
    store = Neo4jLike(None, None, None, SchemaItem("P", ("a",), "P_a_vector", 4, "COSINE"))

    assert (await manager(store).ensure(vector_index("P", "a", 3, "COSINE"))).status == "DRIFT"

    assert store.awaited == []


async def test_on_neo4j_a_recreated_index_is_waited_for():
    store = Neo4jLike(None, None, None, SchemaItem("P", ("a",), "P_a_range"))

    assert (await manager(store).recreate(range_index("P", "a"))).status == "RECREATED"

    assert len(store.awaited) == 1


async def test_on_neo4j_a_failed_build_raises_naming_the_index_giving_neo4js_reason_and_what_ensure_would_give():
    failure = "java.lang.IllegalArgumentException: Property value is too large to index"
    cause = DrivineError("Index `P_a` has failed.")
    store = Neo4jLike("P_a", cause, {"state": "FAILED", "failure": failure})

    with pytest.raises(IndexNotBuilt) as raised:
        await manager(store).ensure(range_index("P", "a"))

    assert str(raised.value) == f"Neo4j RANGE index P_a on P(a) failed to build: {failure}"
    assert raised.value.failed and raised.value.result.status == "CREATED"
    assert raised.value.__cause__ is cause
    assert not [it for it, _ in store.ran if it.startswith("DROP")], "the failed index is left as it is"


async def test_on_neo4j_an_index_still_populating_when_the_wait_ends_raises_saying_so():
    store = Neo4jLike("P_a", DrivineError("timed out"), {"state": "POPULATING", "failure": None})

    with pytest.raises(IndexNotBuilt) as raised:
        await manager(store).ensure(range_index("P", "a"))

    assert str(raised.value) == (
        "Neo4j RANGE index P_a on P(a) is still being built, having waited at most 60 s: timed out"
    )
    assert not raised.value.failed


async def test_on_neo4j_a_wait_shorter_than_a_second_is_asked_of_neo4j_as_0_seconds():
    store = Neo4jLike()

    await manager(store, 0.005).ensure(range_index("P", "a"))

    assert [it["timeout"] for it in store.awaited] == [0]


async def test_on_neo4j_a_wait_that_timed_out_raises_though_the_index_came_online_since():
    timed_out = DrivineError("did not come online within 0 SECONDS")
    store = Neo4jLike("P_a", timed_out, {"state": "ONLINE", "failure": None})

    with pytest.raises(IndexNotBuilt, match="still being built, having waited at most 0.005 s: did not come online"):
        await manager(store, 0.005).ensure(range_index("P", "a"))


async def test_on_neo4j_a_failed_wait_on_an_index_whose_state_cant_be_read_raises_giving_the_failure():
    store = Neo4jLike("P_a", DrivineError("There is no such index P_a"), None)

    with pytest.raises(IndexNotBuilt, match="still being built, having waited at most 60 s: There is no such index"):
        await manager(store).ensure(range_index("P", "a"))


class FalkorDbLike:
    """
    An executor over a FalkorDB, which builds an index in the background: once made, the label's indexes are listed
    with each status in `statuses` in turn, the last one from then on.
    """

    def __init__(self, *statuses: str) -> None:
        self.statuses = list(statuses)
        self.made = False
        self.listings = 0

    @property
    def status(self) -> str:
        return self.statuses[min(self.listings, len(self.statuses)) - 1]

    async def run(self, statement: str | NativeOperation, bindings: Mapping[str, Any] | None = None) -> list[Any]:
        assert isinstance(statement, str)
        if statement.startswith("CREATE"):
            self.made = True
            return []
        if statement.startswith("CALL db.indexes()") and self.made:
            self.listings += 1
            return [{"label": "P", "properties": ["a"], "types": {"a": ["RANGE"]}, "status": self.status}]
        if statement.startswith("CALL db.indexes()"):
            return []
        raise AssertionError(statement)


def falkordb(store: FalkorDbLike, build_timeout: float = BUILD_TIMEOUT) -> IndexManager:
    return IndexManager(store, FALKORDB_SCHEMA, build_timeout=build_timeout)  # pyright: ignore[reportArgumentType]


async def test_on_falkordb_an_index_made_is_polled_until_its_label_is_operational():
    store = FalkorDbLike(
        "[Indexing] 0/20000: UNDER CONSTRUCTION", "[Indexing] 10000/20000: UNDER CONSTRUCTION", "OPERATIONAL"
    )

    assert (await falkordb(store).ensure(range_index("P", "a"))).status == "CREATED"

    assert store.status == "OPERATIONAL"


async def test_on_falkordb_an_index_still_under_construction_when_the_wait_ends_raises_giving_its_last_status():
    store = FalkorDbLike("[Indexing] 0/20000: UNDER CONSTRUCTION")

    with pytest.raises(IndexNotBuilt) as raised:
        await falkordb(store, 0.05).ensure(range_index("P", "a"))

    assert str(raised.value) == (
        "FalkorDB RANGE index on P(a) is still being built, having waited at most 0.05 s: "
        "it is [Indexing] 0/20000: UNDER CONSTRUCTION"
    )


async def test_on_falkordb_a_failed_build_raises():
    store = FalkorDbLike("FAILED")

    with pytest.raises(IndexNotBuilt, match=r"FalkorDB RANGE index on P\(a\) failed to build: it is FAILED") as raised:
        await falkordb(store).ensure(range_index("P", "a"))

    assert raised.value.failed


def test_falkordb_gives_the_status_of_a_labels_indexes_beside_the_listings_typescript_writes():
    listing = FALKORDB_SCHEMA.list_index_statuses
    assert listing is not None

    def status(text: str) -> SchemaItem:
        [item] = listing.read([{"label": "Chunk", "status": text}])
        assert item.label == "Chunk" and item.status == text
        return item

    building = status("[Indexing] 0/20000: UNDER CONSTRUCTION")
    assert building.building and not building.failed
    built = status("OPERATIONAL")
    assert not built.building and not built.failed
    assert status("FAILED").failed
    listings = (FALKORDB_SCHEMA.list_ranges, FALKORDB_SCHEMA.list_full_texts, FALKORDB_SCHEMA.list_vectors)
    assert not any("status" in it.statement for it in listings), "the golden listings are as they were"


@pytest.mark.parametrize(
    "type", [DatabaseType.MEMGRAPH, DatabaseType.POSTGRES, DatabaseType.ORACLE, DatabaseType.SQLITE]
)
def test_memgraph_and_the_sql_stores_build_an_index_inside_its_ddl_so_nothing_is_waited_for(type):
    grammar = schema_grammar_for(type)
    assert grammar.await_index is None and grammar.list_index_statuses is None


def test_only_neo4j_waits_on_the_server_and_only_falkordb_is_polled():
    assert NEO4J_SCHEMA.await_index is not None and NEO4J_SCHEMA.list_index_statuses is None
    assert FALKORDB_SCHEMA.await_index is None and FALKORDB_SCHEMA.list_index_statuses is not None


def test_the_sync_api_waits_for_an_index_it_made():
    class SyncNeo4jLike(Neo4jLike):
        def run(self, statement: str | NativeOperation, bindings: Mapping[str, Any] | None = None) -> list[Any]:  # pyright: ignore[reportIncompatibleMethodOverride]
            return self.answer(statement, bindings or {})

    store = SyncNeo4jLike()

    assert SyncIndexManager(store, GRAMMAR).ensure(range_index("P", "a")).status == "CREATED"  # pyright: ignore[reportArgumentType]

    assert len(store.awaited) == 1
