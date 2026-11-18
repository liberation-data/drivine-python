"""
The id a load, a delete, a cascade's check, a save's reads and the relationship statements bind, on each SQL store, as
the node holds its id (Drivine for TypeScript's IdKeyed.spec.ts): its JSON in the store's layout, which the shared
module's hasId compares the property with, on Oracle extended JSON's double for a float, read `JSON($id EXTENDED)`. An
id given as text is read as the NodeId field's type first (see node_ids), and text that is no value of it, or a None
id, runs no statement.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any, Literal, TypeVar

import apsw
import pytest

from drivine import (
    CascadeType,
    DatabaseType,
    Direction,
    DrivineError,
    GraphProperty,
    GraphView,
    NodeFragment,
    NodeId,
    NodeStamp,
    QuerySpecification,
    RelateMode,
    Relationship,
    RemovedTargets,
    Root,
    SaveOptions,
    StatelessGraphObjectManager,
    node_ref,
)
from drivine.backend.fragment_group import FragmentGroup
from drivine.backend.graph_backend import GraphBackend
from drivine.backend.matched_node import MatchedNode
from drivine.backend.oracle.id_keyed_statement import held_id
from drivine.backend.oracle.oracle_backend import OracleBackend
from drivine.backend.oracle.oracle_graph import OracleGraph
from drivine.backend.postgres.postgres_backend import PostgresBackend
from drivine.backend.postgres.postgres_graph import PostgresGraph
from drivine.backend.postgres.postgres_save_builder import numeric_text, stored_key
from drivine.backend.save_statement import SaveStatement
from drivine.backend.sql.fragments import FRAGMENT_ROOT_ROW
from drivine.backend.sql.node_ids import NO_NODE, read_back_key, typed_id
from drivine.backend.sql.sql_json import to_json
from drivine.backend.sql.value_layout import write
from drivine.backend.sqlite.sqlite_backend import SqliteBackend
from drivine.backend.sqlite.sqlite_graph import SqliteGraph, text_of
from drivine.connection.falkordb.falkordb_parameter_coercer import FalkorDbParameterCoercer
from drivine.cursor.cursor import Cursor
from drivine.cursor.cursor_specification import CursorSpecification
from drivine.cypher.shared_compiler import id_sql
from drivine.manager.patch_result import PatchResult
from drivine.manager.persistence_manager import PersistenceManager
from drivine.model.model_registry import ModelRegistry

T = TypeVar("T")

ONE_ID = "(SELECT JSON(:1 EXTENDED) v FROM dual) i"
"""The row an Oracle write block's find binds its one id in, as `i.v`, read EXTENDED."""


def write_has_id(key: str, alias: str = "f") -> str:
    """The comparison an Oracle write block makes of the node `alias`'s id `key` with the bound row's `i.v`."""
    return id_sql("oracle", "hasId", alias, key, "i.v")


class Gauge(NodeFragment, labels=["Gauge"]):
    id: Annotated[float, NodeId()]
    name: str | None = None


class GaugeTree(GraphView):
    """A view that contains itself, so a cascading delete of it is checked first (cascade_depth_checks)."""

    gauge: Annotated[Gauge, Root()]
    parts: Annotated[list[GaugeTree], Relationship("PART_OF", Direction.INCOMING, max_depth=1)] = []


class Recording(PersistenceManager):
    """Records each statement it runs, with its parameters, and finds nothing: a count or a delete of 0."""

    def __init__(self, type_: DatabaseType) -> None:
        self.database = "id-keyed"
        self.type = type_
        self.bound: list[tuple[str, dict[str, Any]]] = []

    async def query(self, spec: QuerySpecification[T]) -> list[T]:
        assert spec.statement is not None
        self.bound.append((spec.statement.text, dict(spec.parameters) if isinstance(spec.parameters, dict) else {}))
        return []

    async def get_one(self, spec: QuerySpecification[T]) -> T:
        await self.query(spec)
        return 0  # type: ignore[return-value]

    async def open_cursor(self, spec: CursorSpecification[T]) -> Cursor[T]:
        raise NotImplementedError


def test_stored_key_on_postgres_is_a_numbers_text_as_jsonb_gives_it_back_numerics_with_no_exponent():
    given = ["1.0E-7", "-1.5E-10", "1.230e-5", "2.0", "12", "0.1", "-0.0", "1.0E+2", '"1.0E-7"', "[1]"]
    assert [numeric_text(it) for it in given] == [
        "0.00000010",
        "-0.00000000015",
        "0.00001230",
        "2.0",
        "12",
        "0.1",
        "0.0",
        "100",
        '"1.0E-7"',
        "[1]",
    ]
    assert [stored_key(it, "postgres") for it in (1e-7, 4.5, 7, "a1e-7", True)] == [
        "0.00000010",
        "4.5",
        "7",
        "a1e-7",
        "true",
    ]


def test_stored_key_is_a_temporal_ids_tag_text_as_the_module_reads_a_stored_targets_id_back():
    moment = datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert [stored_key(moment, "postgres"), stored_key(moment, "sqlite")] == ["2024-01-02T03:04:05Z"] * 2
    assert stored_key(date(2024, 1, 2), "sqlite") == "2024-01-02"


def test_stored_key_is_a_uuid_ids_text_unquoted_as_the_module_reads_a_stored_targets_text_id_back():
    key = uuid.UUID("12345678-9abc-def0-1234-56789abcdef0")
    assert [stored_key(key, "postgres"), stored_key(key, "sqlite")] == [str(key)] * 2


def test_stored_key_on_sqlite_is_the_json_text_the_module_writes_which_sqlite_reads_the_property_back_as():
    assert [stored_key(it, "sqlite") for it in (1e-7, 1e21, float("inf"), 4.5, 7)] == [
        "1.0E-7",
        "1.0E21",
        "9e999",
        "4.5",
        "7",
    ]
    db = apsw.Connection(":memory:")
    try:
        props = json.dumps({"id": 0}).replace("0", stored_key(1e-7, "sqlite"))
        [(found,)] = db.execute(
            f"SELECT count(*) FROM (SELECT ? AS props) n WHERE {text_of('n', 'id')} = CAST(? AS TEXT)",
            (props, stored_key(1e-7, "sqlite")),
        ).fetchall()
        assert found == 1
    finally:
        db.close()


def manager(type_: DatabaseType) -> tuple[StatelessGraphObjectManager, Recording]:
    pm = Recording(type_)
    return StatelessGraphObjectManager(pm), pm


async def loaded_and_deleted(gom: StatelessGraphObjectManager) -> None:
    """A load and a delete by the float id 1e-7, of a fragment and of a view, the view's delete a cascade."""
    await gom.load(Gauge, 1e-7)
    await gom.load(GaugeTree, 1e-7)
    await gom.delete(Gauge, 1e-7)
    await gom.delete(GaugeTree, 1e-7, cascade=CascadeType.DELETE_ALL)


SQL_KEYED = [
    (DatabaseType.POSTGRES, lambda: PostgresBackend(PostgresGraph())),
    (DatabaseType.SQLITE, lambda: SqliteBackend(SqliteGraph())),
]
"""
Postgres and SQLite, each binding a node's float id 1e-7 as the JSON the module writes it as, `1.0E-7`, which the
module's hasId compares the property's JSON with: Python's text of it is `1e-07`.
"""

FLOAT_JSON = "1.0E-7"


@pytest.mark.parametrize(("type_", "backend"), SQL_KEYED)
async def test_a_float_id_is_bound_by_a_load_a_delete_and_a_cascade_as_the_json_the_node_holds_it_as(
    type_: DatabaseType, backend: Any
):
    gom, pm = manager(type_)
    await loaded_and_deleted(gom)
    # The cascade's check of its depth, and the cascade itself.
    assert [it["id"] for _, it in pm.bound] == [FLOAT_JSON] * 5


@pytest.mark.parametrize(("type_", "backend"), SQL_KEYED)
def test_a_float_id_is_bound_by_a_saves_reads_of_the_nodes_properties_and_stamp_as_the_json_the_node_holds_it_as(
    type_: DatabaseType, backend: Any
):
    store: GraphBackend = backend()
    model = ModelRegistry().fragment(Gauge)
    held = store.held_properties(model).spec({"id": 1e-7}).parameters
    found = store.found_stamp(model).spec({"id": 1e-7}).parameters
    assert [held["id"], found["id"]] == [FLOAT_JSON, FLOAT_JSON]  # type: ignore[index]


@pytest.mark.parametrize(("type_", "backend"), SQL_KEYED)
async def test_a_float_id_is_bound_by_load_related_as_the_json_the_node_holds_it_as(type_: DatabaseType, backend: Any):
    gom, pm = manager(type_)
    await gom.edges.load_related(node_ref(Gauge, 1e-7), "POINTS", Gauge)
    assert [it["x_from"] for _, it in pm.bound] == [FLOAT_JSON]


@pytest.mark.parametrize(("type_", "backend"), SQL_KEYED)
async def test_a_text_id_is_read_as_the_float_a_float_id_is_and_a_null_id_runs_nothing(
    type_: DatabaseType, backend: Any
):
    gom, pm = manager(type_)
    await gom.load(Gauge, "1e-7")
    await gom.load(Gauge, None)
    assert [it["id"] for _, it in pm.bound] == [FLOAT_JSON]


ORACLE_HELD = '{"$numberDouble":"1.0E-7"}'
"""The id 1e-7 as Oracle's layout holds it, a binary double: extended JSON's double."""

ORACLE_HAS_ID = id_sql("oracle", "hasId", "n", "id", "JSON($id EXTENDED)")
"""The comparison an Oracle lookup by `$id` makes of Gauge's id, as the shared module writes it."""


async def test_a_float_id_on_oracle_is_bound_by_a_load_a_delete_and_a_cascade_as_the_json_oracle_holds_it_as():
    # A NUMBER bound as it is, 1e-7, is not always equal to the binary double the node holds.
    gom, pm = manager(DatabaseType.ORACLE)
    await loaded_and_deleted(gom)
    assert [it["id"] for _, it in pm.bound] == [ORACLE_HELD] * 5
    assert all(ORACLE_HAS_ID in text for text, _ in pm.bound)


def test_oracle_binds_a_saves_reads_of_the_nodes_properties_and_stamp_as_the_json_oracle_holds_the_id_as():
    backend = OracleBackend(OracleGraph())
    model = ModelRegistry().fragment(Gauge)
    for statement in (backend.held_properties(model), backend.found_stamp(model)):
        assert ORACLE_HAS_ID in statement.text
        assert statement.spec({"id": 1e-7}).parameters["id"] == ORACLE_HELD  # type: ignore[index]


def test_oracle_binds_the_ends_of_load_related_unrelate_all_and_relate_as_the_json_oracle_holds_the_id_as():
    backend = OracleBackend(OracleGraph())
    model = ModelRegistry().fragment(Gauge)
    origin = MatchedNode(model, 1e-7, ())
    related = backend.related(origin, "POINTS", Direction.OUTGOING, model)
    assert id_sql("oracle", "hasId", "a", "id", "JSON($x_from EXTENDED)") in related.text
    assert related.spec().parameters["x_from"] == ORACLE_HELD  # type: ignore[index]
    unrelated = backend.unrelate_all(origin, "POINTS", Direction.OUTGOING).spec().parameters
    assert unrelated["x_from"] == ORACLE_HELD  # type: ignore[index]
    relate: Any = backend.relate(origin, MatchedNode(model, 4.5, ()), "POINTS", {}, RelateMode.MERGE).spec().parameters
    assert [relate["x_from"], relate["x_to"]] == [ORACLE_HELD, '{"$numberDouble":"4.5"}']
    assert f"{ONE_ID} CROSS JOIN drivine_node f WHERE {write_has_id('id')}" in relate["x_frommatch"]


def test_oracle_binds_the_null_ends_of_related_unrelate_all_relate_and_unrelate_as_they_are_so_they_find_no_node():
    # Bound as the JSON text `null`, a block's find_node would match a node holding a null id (drivine#772).
    assert held_id(None) is None
    backend = OracleBackend(OracleGraph())
    model = ModelRegistry().fragment(Gauge)
    origin = MatchedNode(model, None, ())
    related: Any = backend.related(origin, "POINTS", Direction.OUTGOING, model).spec().parameters
    assert related["x_from"] is None
    unrelated: Any = backend.unrelate_all(origin, "POINTS", Direction.OUTGOING).spec().parameters
    assert unrelated["x_from"] is None
    relate: Any = backend.relate(origin, MatchedNode(model, None, ()), "POINTS", {}, RelateMode.MERGE).spec().parameters
    assert [relate["x_from"], relate["x_to"]] == [None, None]
    unrelate: Any = backend.unrelate(origin, MatchedNode(model, None, ()), "POINTS").spec().parameters
    assert [unrelate["x_from"], unrelate["x_to"]] == [None, None]
    assert backend.load_fragment(model).spec({"id": None}).parameters["id"] is None  # type: ignore[index]


async def test_oracle_reads_a_text_id_as_the_float_a_float_id_is_and_runs_nothing_for_a_null_id():
    gom, pm = manager(DatabaseType.ORACLE)
    await gom.load(Gauge, "1e-7")
    await gom.load(Gauge, 7)
    await gom.load(Gauge, None)
    assert [it["id"] for _, it in pm.bound] == [ORACLE_HELD, "7"]


def test_oracle_binds_a_patchs_id_and_each_of_a_patch_alls_row_ids_as_the_json_oracle_holds_the_id_as():
    # A patch finds its node as a save does, by the id read `JSON(:1 EXTENDED)`: a float bound as a NUMBER, or as
    # Python's JSON text of it, is not the binary double held.
    backend = OracleBackend(OracleGraph())
    model = ModelRegistry().fragment(Gauge)
    one: Any = backend.patch_one(model).spec({"id": 1e-7, "set": {"name": "a"}, "vectors": {}}).parameters
    assert one["id"] == ORACLE_HELD
    assert backend.patch_one(model).spec({"id": "a", "set": {}, "vectors": {}}).parameters["id"] == '"a"'  # type: ignore[index]
    rows = [{"id": 1e-7, "props": {"name": "a"}, "vectors": {}}, {"id": 7, "props": {"name": "b"}, "vectors": {}}]
    many: Any = backend.patch_many(model).spec({"rows": rows}).parameters
    assert to_json([it["id"] for it in many["rows"]]) == f"[{ORACLE_HELD},7]"


async def test_oracle_runs_no_statement_for_a_patchs_null_id_and_its_block_binds_one_as_it_is():
    # A None id runs no statement at all; the block itself, reached with one, binds it as it is, never as `null`.
    gom, pm = manager(DatabaseType.ORACLE)
    await gom.patch(Gauge, None, set={"name": "x"})
    # The recording finds nothing, which a patch can't read a result from: the statement it bound is what is checked.
    with contextlib.suppress(TypeError):
        await gom.patch(Gauge, 1e-7, set={"name": "x"})
    assert [it["id"] for _, it in pm.bound] == [ORACLE_HELD]
    backend = OracleBackend(OracleGraph())
    model = ModelRegistry().fragment(Gauge)
    assert backend.patch_one(model).spec({"id": None, "set": {}, "vectors": {}}).parameters["id"] is None  # type: ignore[index]


# ----- A lookup by id on each SQL store -----


class Dial(NodeFragment, labels=["Dial", "Meter"]):
    id: Annotated[int, NodeId()]
    name: str | None = None


class Coded(NodeFragment, labels=["Coded"]):
    code: Annotated[str, NodeId()]


class Dated(NodeFragment, labels=["Dated"]):
    on: Annotated[datetime, NodeId()]


class Day(NodeFragment, labels=["Day"]):
    on: Annotated[date, NodeId()]


class Clock(NodeFragment, labels=["Clock"]):
    at: Annotated[time | None, NodeId()] = None


class Priced(NodeFragment, labels=["Priced"]):
    """A `$`-named NodeId, which the store layout holds under `$$code`."""

    code: Annotated[str, NodeId(), GraphProperty("$code")]


class Flagged(NodeFragment, labels=["Flagged"]):
    on: Annotated[bool, NodeId()]


class Costed(NodeFragment, labels=["Costed"]):
    cost: Annotated[Decimal, NodeId()]


class Keyed(NodeFragment, labels=["Keyed"]):
    key: Annotated[uuid.UUID, NodeId()]


class DialView(GraphView):
    dial: Annotated[Dial, Root()]
    codes: Annotated[list[Coded], Relationship("READS")] = []


@dataclass(frozen=True)
class LookupStore:
    """
    A SQL store a lookup by id is written for: its backend, the SQL its id is read as, the row a load and a delete by id
    read the node as, and its test of Dial's labels on a row.
    """

    type: DatabaseType
    store: Literal["postgres", "sqlite", "oracle"]
    backend: Callable[[], GraphBackend]
    json: Callable[[str], str]
    row: str
    labels: Callable[[str], str]

    def __repr__(self) -> str:
        return self.store


LOOKUP_STORES = [
    LookupStore(
        DatabaseType.POSTGRES,
        "postgres",
        lambda: PostgresBackend(PostgresGraph()),
        lambda parameter: parameter,
        # A load and a delete read the node as the query DSL's row, which a fragment's variable ends with `_`.
        "n_",
        lambda row: f"""{row}.labels @> '["Dial"]' AND {row}.labels @> '["Meter"]'""",
    ),
    LookupStore(
        DatabaseType.SQLITE,
        "sqlite",
        lambda: SqliteBackend(SqliteGraph()),
        lambda parameter: parameter,
        # A load and a delete read the node as the query DSL's row, which a fragment's variable ends with `_`.
        FRAGMENT_ROOT_ROW,
        lambda row: f"""instr({row}.labels, '"Dial"') > 0 AND instr({row}.labels, '"Meter"') > 0""",
    ),
    LookupStore(
        DatabaseType.ORACLE,
        "oracle",
        lambda: OracleBackend(OracleGraph()),
        lambda parameter: f"JSON({parameter} EXTENDED)",
        "n",
        lambda row: (
            f"""INSTR(',' || LTRIM(RTRIM(JSON_SERIALIZE({row}.labels), ']'), '[') || ',"', ',"Dial","') > 0 AND """
            f"""INSTR(',' || LTRIM(RTRIM(JSON_SERIALIZE({row}.labels), ']'), '[') || ',"', ',"Meter","') > 0"""
        ),
    ),
]
"""
Each store's lookup of a node by its id: the shared module's hasId over the id as the node holds it (id_sql), beside a
test of every label in the form drivine4j writes beside it, and the id bound as its JSON in the store's layout.
"""

NOTHING = PatchResult(False, False, False)


def ids(pm: Recording) -> list[Any]:
    """The id each statement run bound, `$id` or a relationship's `$x_from`."""
    return [it.get("id", it.get("x_from")) for _, it in pm.bound]


@pytest.mark.parametrize("lookup", LOOKUP_STORES, ids=repr)
class TestALookupById:
    def test_compares_the_id_as_the_module_writes_it_beside_a_test_of_each_label(self, lookup: LookupStore):
        registry = ModelRegistry()
        dial, view = registry.fragment(Dial), registry.view(DialView)
        backend = lookup.backend()
        row = lookup.row
        # A write's reads, held_properties and found_stamp, read the node as `n` on every store.
        statements = [
            (backend.load_fragment(dial), row),
            (backend.load_view(view), row),
            (backend.delete(dial), row),
            (backend.delete(view, None, CascadeType.DELETE_ALL), row),
            (backend.held_properties(dial), "n"),
            (backend.found_stamp(dial), "n"),
            *((it.statement, row) for it in backend.cascade_depth_checks(view)),
        ]
        for statement, alias in statements:
            has_id = id_sql(lookup.store, "hasId", alias, "id", lookup.json("$id"))
            labels = lookup.labels(alias)
            # A view's requirements are joined to the id's test, which is in parentheses of its own there.
            text = statement.text.replace(f"{labels} AND (({has_id}", f"{labels} AND ({has_id}")
            assert f"{labels} AND ({has_id}" in text

    def test_finds_the_node_load_related_is_from_by_its_id_beside_each_of_its_labels(self, lookup: LookupStore):
        registry = ModelRegistry()
        related = lookup.backend().related(
            MatchedNode(registry.fragment(Dial), 7, ()), "READS", Direction.OUTGOING, registry.fragment(Coded)
        )
        has_id = id_sql(lookup.store, "hasId", "a", "id", lookup.json("$x_from"))
        assert f"{has_id} AND {lookup.labels('a')}" in related.text
        assert related.spec().parameters["x_from"] == "7"  # type: ignore[index]

    async def test_binds_a_number_a_date_a_moment_a_text_and_a_bool_id_as_the_json_the_node_holds_each_as(
        self, lookup: LookupStore
    ):
        gom, pm = manager(lookup.type)
        moment = datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
        await gom.load(Dial, 7)
        await gom.load(Dated, moment)
        await gom.load(Day, date(2024, 1, 2))
        await gom.load(Coded, "abc")
        await gom.load(Flagged, True)
        assert ids(pm) == ["7", layout(moment, lookup), layout(date(2024, 1, 2), lookup), '"abc"', "true"]
        tag = json.loads(ids(pm)[1])
        assert (tag["$t"], tag["v"]) == ("datetime", "2024-01-02T03:04:05Z")

    async def test_reads_six_as_the_int_an_int_id_is_and_keeps_it_the_text_a_str_id_is(self, lookup: LookupStore):
        gom, pm = manager(lookup.type)
        await gom.load(Dial, "6")
        await gom.load(Coded, "6")
        await gom.edges.load_related(node_ref(Dial, "6"), "READS", Coded)
        await gom.load(Dial, "-0")
        assert ids(pm) == ["6", '"6"', "6", "0"]
        # Whole-number text alone, as drivine4j's Jackson reads a Long field's: "6.0" and "6e1" too are refused there.
        for nothing in ("6.0", "6e1", "6.5", " 6", "+6", "06", "0x6", "", "six", "1e", "1\u0666"):
            assert await gom.load(Dial, nothing) is None, nothing
        assert len(pm.bound) == 4

    async def test_reads_json_number_text_alone_as_the_float_a_float_id_is(self, lookup: LookupStore):
        gom, pm = manager(lookup.type)
        await gom.load(Gauge, "6")
        await gom.load(Gauge, "6e1")
        for nothing in ("1\u0666", "1e999", "NaN", " 6"):
            assert await gom.load(Gauge, nothing) is None, nothing
        assert len(pm.bound) == 2

    async def test_reads_iso_text_as_the_date_moment_or_time_the_id_is(self, lookup: LookupStore):
        gom, pm = manager(lookup.type)
        await gom.load(Dated, "2024-01-02T03:04:05Z")
        await gom.load(Dated, "2024-01-02T03:04:05")
        await gom.load(Day, "2024-02-29")
        await gom.load(Clock, "10:00")
        await gom.load(Clock, "10:00:30.5+01:00")
        assert ids(pm) == [
            layout(datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC), lookup),
            layout(datetime(2024, 1, 2, 3, 4, 5), lookup),
            layout(date(2024, 2, 29), lookup),
            layout(time(10, 0), lookup),
            layout(time(10, 0, 30, 500000, tzinfo=timezone(timedelta(hours=1))), lookup),
        ]

    async def test_runs_nothing_for_iso_text_of_no_calendar_day_or_hour_or_in_a_form_a_save_doesnt_write(
        self, lookup: LookupStore
    ):
        # `fromisoformat` alone reads `T24:00` as the next day, `24:00` as midnight, and the basic and week forms.
        gom, pm = manager(lookup.type)
        for nothing in ("2024-02-30", "2023-02-29", "20240101", "2024-W01-1", "2024-01-01T10:00:00", "not a day"):
            assert await gom.load(Day, nothing) is None, nothing
        for nothing in (
            "2024-04-31T00:00:00Z",
            "2024-01-01T24:00:00Z",
            "2024-01-01 10:00",
            "2024-01-01T10",
            "2024-01-01",
        ):
            assert await gom.load(Dated, nothing) is None, nothing
        for nothing in ("24:00", "1000", "10", "10:60", "10:00:00.1234567"):
            assert await gom.load(Clock, nothing) is None, nothing
        assert pm.bound == []

    async def test_reads_a_bool_a_decimal_and_a_uuid_ids_text_as_its_value_running_nothing_for_other_text(
        self, lookup: LookupStore
    ):
        gom, pm = manager(lookup.type)
        key = uuid.UUID("12345678-9abc-def0-1234-56789abcdef0")
        await gom.load(Flagged, "true")
        await gom.load(Flagged, "false")
        await gom.load(Costed, "2.50")
        await gom.load(Keyed, "12345678-9ABC-DEF0-1234-56789ABCDEF0")
        assert ids(pm) == ["true", "false", layout(Decimal("2.50"), lookup), f'"{key}"']
        for type_, nothing in (
            (Flagged, "yes"),
            (Flagged, "True"),
            (Costed, "NaN"),
            (Costed, "two"),
            (Keyed, "12345678123456781234567812345678"),
            (Keyed, "not a uuid"),
        ):
            assert await gom.load(type_, nothing) is None, nothing
        assert len(pm.bound) == 4

    async def test_runs_no_statement_for_text_that_is_no_value_of_the_id_logging_it_at_debug_alone(
        self, lookup: LookupStore, caplog: pytest.LogCaptureFixture
    ):
        gom, pm = manager(lookup.type)
        nowhere, coded = node_ref(Dial, "abc"), node_ref(Coded, "x")
        with caplog.at_level(logging.DEBUG, logger="drivine"):
            assert await gom.load(Dial, "abc") is None
            assert await gom.load(DialView, "abc") is None
            assert await gom.delete(Dial, "abc") == 0
            assert await gom.patch(Dial, "abc", set={"name": "x"}) == NOTHING
            assert await gom.update(Dial, "abc", lambda it: it) is None
            assert await gom.edges.load_related(nowhere, "READS", Coded) == []
            assert not await gom.edges.relate(nowhere, coded, "READS")
            assert not await gom.edges.relate(coded, nowhere, "READS")
            assert await gom.edges.unrelate(nowhere, coded, "READS") == 0
            assert await gom.edges.unrelate_all(nowhere, "READS") == 0
            assert await gom.load(Dated, "not a day") is None
        assert pm.bound == []
        said = [it for it in caplog.records if it.name == "drivine.node_ids"]
        assert "No Dial is looked for by the id 'abc': it is no value of id's type, so no node has it." in [
            it.getMessage() for it in said
        ]
        assert {it.levelno for it in said} == {logging.DEBUG}
        assert [it for it in caplog.records if it.levelno >= logging.WARNING] == []

    async def test_runs_no_statement_and_logs_nothing_for_a_patch_a_load_or_a_delete_by_a_null_id(
        self, lookup: LookupStore, caplog: pytest.LogCaptureFixture
    ):
        gom, pm = manager(lookup.type)
        with caplog.at_level(logging.DEBUG, logger="drivine"):
            assert await gom.patch(Coded, None, set={}) == NOTHING
            assert await gom.load(Coded, None) is None
            assert await gom.delete(Coded, None) == 0
            assert not await gom.edges.relate(node_ref(Coded, None), node_ref(Coded, "x"), "READS")
        assert pm.bound == []
        assert [it for it in caplog.records if it.name == "drivine.node_ids"] == []

    async def test_refuses_a_patch_of_a_field_no_patch_writes_whatever_its_id_finds(self, lookup: LookupStore):
        gom, pm = manager(lookup.type)
        for id_ in (None, "abc", 7):
            with pytest.raises(DrivineError, match="id"):
                await gom.patch(Dial, id_, set={"id": 5})
        assert pm.bound == []

    def test_finds_a_node_of_a_dollar_named_id_by_the_key_the_layout_holds_it_under(self, lookup: LookupStore):
        priced = ModelRegistry().fragment(Priced)
        has_id = id_sql(lookup.store, "hasId", lookup.row, "$$code", lookup.json("$id"))
        assert has_id in lookup.backend().load_fragment(priced).text


class StampedPriced(NodeFragment, labels=["StampedPriced"]):
    code: Annotated[str, NodeId(), GraphProperty("$code")]
    stamp: Annotated[str | None, NodeStamp()] = None


class Prices(GraphView):
    """A root of a `$`-named id holding a list of stamped nodes of one, which a save trims and reads the stamps of."""

    priced: Annotated[Priced, Root()]
    prices: Annotated[list[StampedPriced], Relationship("COSTS")] = []


def test_oracles_write_blocks_name_a_dollar_named_id_by_the_key_the_layout_holds_it_under():
    # The layout holds `$code` as `$$code`: a block naming it `$code` finds no node, and a second save makes a second.
    registry = ModelRegistry()
    backend = OracleBackend(OracleGraph())
    priced, stamped = registry.fragment(Priced), registry.fragment(StampedPriced)
    prices = Prices(priced=Priced(code="p1"), prices=[StampedPriced(code="s1")])
    statements = {
        "save": backend.save(priced, Priced(code="p1"), None, False, registry),
        "save.view": backend.save(registry.view(Prices), prices, SaveOptions(replace=["prices"]), False, registry),
        "fragment_batch": backend.fragment_batch([FragmentGroup(stamped, [StampedPriced(code="s1")])], SaveOptions()),
        "relate": backend.relate(
            MatchedNode(priced, "p1", ()), MatchedNode(stamped, "s1", ()), "COSTS", {}, RelateMode.MERGE
        ),
        "patch": backend.patch_one(priced),
        "related": backend.related(MatchedNode(priced, "p1", ()), "COSTS", Direction.OUTGOING, stamped),
    }
    for name, statement in statements.items():
        # A block names the node it finds in its text, or in a parameter that holds the SQL it is found by.
        parameters: Any = statement.spec().parameters
        text = "\n".join([statement.text, *(it for it in parameters.values() if isinstance(it, str))])
        assert '"$$code"' in text, name
        assert '"$code"' not in text, name


def layout(value: Any, lookup: LookupStore) -> str:
    """`value` as the JSON text the store's layout holds it as, which a lookup binds."""
    return to_json(write(value, lookup.store))


def test_id_sql_raises_the_modules_refusal_by_the_helper_and_the_store():
    with pytest.raises(DrivineError, match="The shared module wrote no hasId for sqlite: "):
        id_sql("sqlite", "hasId", "n", "id")


async def test_a_lookup_by_id_on_a_graph_engine_binds_text_as_it_is_given_for_the_engine_compares_it():
    gom, pm = manager(DatabaseType.NEO4J)
    await gom.load(Dial, "6")
    await gom.load(Dial, "abc")
    assert [it["id"] for _, it in pm.bound] == ["6", "abc"]


def test_typed_id_reads_text_for_an_optional_id_as_its_type_and_keeps_a_value_as_it_is():
    clock = ModelRegistry().fragment(Clock)
    assert typed_id(clock, "10:00") == time(10, 0)
    assert typed_id(clock, time(11, 0)) == time(11, 0)
    assert typed_id(clock, None) is NO_NODE
    assert typed_id(ModelRegistry().fragment(Dial), 7.5) == 7.5


class Either(NodeFragment, labels=["Either"]):
    ref: Annotated[int | str, NodeId()]


def test_typed_id_keeps_text_as_it_is_given_for_an_id_of_more_than_one_type():
    # Read as no one type: "6" may be the text the id is, so it isn't taken for the int.
    either = ModelRegistry().fragment(Either)
    assert typed_id(either, "6") == "6"
    assert typed_id(either, "abc") == "abc"
    assert typed_id(either, None) is NO_NODE


class StampedDay(NodeFragment, labels=["StampedDay"]):
    on: Annotated[date, NodeId()]
    stamp: Annotated[str | None, NodeStamp()] = None


def test_a_batch_hands_each_stamped_node_its_stamp_by_its_date_ids_text_as_the_store_reads_it_back():
    # Oracle's fragment batch reads each node's id back from its tag, as the tag's text, where the object holds a date.
    model = ModelRegistry().fragment(StampedDay)
    days = [StampedDay(on=date(2024, 1, 3)), StampedDay(on=date(2024, 1, 3))]
    stamp = "a" * 16 + ":" + "b" * 16
    result = {"groups": [{"before": [], "after": [{"id": "2024-01-03", "stamp": stamp}]}]}
    StatelessGraphObjectManager._hand_back_fragments(None, [FragmentGroup(model, days)], result)  # pyright: ignore[reportArgumentType]
    assert [it.stamp for it in days] == [stamp, stamp]


class StampedKey(NodeFragment, labels=["StampedKey"]):
    id: Annotated[uuid.UUID, NodeId()]
    stamp: Annotated[str | None, NodeStamp()] = None


def test_a_batch_hands_each_stamped_node_its_stamp_by_its_uuid_ids_text_as_the_store_reads_it_back():
    # Oracle's fragment batch reads a UUID id back as its text, where the object holds a UUID.
    model = ModelRegistry().fragment(StampedKey)
    keys = [StampedKey(id=uuid.UUID(int=7))]
    stamp = "a" * 16 + ":" + "b" * 16
    result = {"groups": [{"before": [], "after": [{"id": str(uuid.UUID(int=7)), "stamp": stamp}]}]}
    StatelessGraphObjectManager._hand_back_fragments(None, [FragmentGroup(model, keys)], result)  # pyright: ignore[reportArgumentType]
    assert keys[0].stamp == stamp


class StampedDays(GraphView):
    target: Annotated[StampedKey, Root()]
    days: Annotated[list[StampedDay], Relationship("ON_DAY")] = []


def test_a_save_hands_each_stamped_related_node_its_stamp_by_its_date_ids_text_as_the_store_reads_it_back():
    # Oracle's save reads each stamped related node's id back from its tag, as the tag's text, as a batch does.
    model = ModelRegistry().fragment(StampedDay)
    view = StampedDays(target=StampedKey(id=uuid.UUID(int=7)), days=[StampedDay(on=date(2024, 1, 3))])
    stamp = "a" * 16 + ":" + "b" * 16
    statement = SaveStatement("", {}, stamped_models=[model])
    result = {"related": [{"before": [], "after": [{"id": "2024-01-03", "stamp": stamp}]}]}
    StatelessGraphObjectManager._hand_back(None, view, statement, result)  # pyright: ignore[reportArgumentType]
    assert view.days[0].stamp == stamp


class StampedSpan(NodeFragment, labels=["StampedSpan"]):
    span: Annotated[timedelta, NodeId()]
    stamp: Annotated[str | None, NodeStamp()] = None


def test_a_batch_hands_each_stamped_node_its_stamp_by_its_duration_ids_text_as_the_store_reads_it_back():
    # A duration is held as its tag too, which Oracle's fragment batch reads back as the tag's text, as it does a date.
    model = ModelRegistry().fragment(StampedSpan)
    spans = [StampedSpan(span=timedelta(days=1, seconds=5))]
    stamp = "a" * 16 + ":" + "b" * 16
    result = {"groups": [{"before": [], "after": [{"id": "P1DT5S", "stamp": stamp}]}]}
    StatelessGraphObjectManager._hand_back_fragments(None, [FragmentGroup(model, spans)], result)  # pyright: ignore[reportArgumentType]
    assert spans[0].stamp == stamp


class Shade(Enum):
    DARK = "dark"


class StampedShade(NodeFragment, labels=["StampedShade"]):
    shade: Annotated[Shade, NodeId()]
    stamp: Annotated[str | None, NodeStamp()] = None


def test_a_batch_hands_each_stamped_node_its_stamp_by_its_enum_ids_value_as_the_store_reads_it_back():
    # A plain Enum is written as its value, which Oracle's fragment batch reads back, and is equal to no member.
    model = ModelRegistry().fragment(StampedShade)
    shades = [StampedShade(shade=Shade.DARK)]
    stamp = "a" * 16 + ":" + "b" * 16
    result = {"groups": [{"before": [], "after": [{"id": "dark", "stamp": stamp}]}]}
    StatelessGraphObjectManager._hand_back_fragments(None, [FragmentGroup(model, shades)], result)  # pyright: ignore[reportArgumentType]
    assert shades[0].stamp == stamp


def test_an_id_is_read_back_as_the_store_reads_it_a_decimal_with_a_fraction_as_the_float_it_is_held_as():
    assert read_back_key(Decimal("0.1")) == 0.1
    assert read_back_key(Decimal("3")) == 3
    assert read_back_key(time(4, 5, tzinfo=timezone(timedelta(hours=2)))) == "04:05:00+02:00"
    assert read_back_key(True) is True


class StampedAmount(NodeFragment, labels=["StampedAmount"]):
    id: Annotated[Decimal, NodeId()]
    stamp: Annotated[str | None, NodeStamp()] = None


def test_a_batch_hands_two_ids_that_read_back_as_one_float_each_its_own_stamp_where_the_store_gives_them_as_given():
    # Postgres and SQLite give each id back as the object gave it, and hold these two as two nodes.
    model = ModelRegistry().fragment(StampedAmount)
    amounts = [StampedAmount(id=Decimal("0.1")), StampedAmount(id=Decimal("0.10000000000000000001"))]
    first, second = "a" * 16 + ":" + "b" * 16, "c" * 16 + ":" + "d" * 16
    after = [{"id": amounts[0].id, "stamp": first}, {"id": amounts[1].id, "stamp": second}]
    result = {"groups": [{"before": [], "after": after}]}
    StatelessGraphObjectManager._hand_back_fragments(None, [FragmentGroup(model, amounts)], result)  # pyright: ignore[reportArgumentType]
    assert [it.stamp for it in amounts] == [first, second]


class StampedAmounts(GraphView):
    target: Annotated[StampedKey, Root()]
    amounts: Annotated[list[StampedAmount], Relationship("OF_AMOUNT")] = []


def test_a_save_hands_two_related_ids_that_read_back_as_one_float_each_its_own_stamp_where_given_back_as_given():
    # Postgres and SQLite give each related id back as the object gave it, and hold these two as two nodes.
    model = ModelRegistry().fragment(StampedAmount)
    amounts = [StampedAmount(id=Decimal("0.1")), StampedAmount(id=Decimal("0.10000000000000000001"))]
    view = StampedAmounts(target=StampedKey(id=uuid.UUID(int=7)), amounts=amounts)
    first, second = "a" * 16 + ":" + "b" * 16, "c" * 16 + ":" + "d" * 16
    statement = SaveStatement("", {}, stamped_models=[model])
    after = [{"id": amounts[0].id, "stamp": first}, {"id": amounts[1].id, "stamp": second}]
    StatelessGraphObjectManager._hand_back(None, view, statement, {"related": [{"before": [], "after": after}]})  # pyright: ignore[reportArgumentType]
    assert [it.stamp for it in view.amounts] == [first, second]


class PatchedDay(NodeFragment, labels=["PatchedDay"]):
    on: Annotated[date, NodeId()]
    value: int | None = None


class Echoing(PersistenceManager):
    """Gives each row's id back as FalkorDB does, as the parameter it was sent as: a date as its text."""

    def __init__(self) -> None:
        self.database = "echoing"
        self.type = DatabaseType.FALKORDB

    async def query(self, spec: QuerySpecification[T]) -> list[T]:
        rows: list[Any] = FalkorDbParameterCoercer().coerce(dict(spec.parameters))["rows"]  # type: ignore[arg-type]
        return [{"id": row["id"], "found": True, "changed": True} for row in rows]  # type: ignore[return-value]

    async def open_cursor(self, spec: CursorSpecification[T]) -> Cursor[T]:
        raise NotImplementedError


async def test_patch_all_hands_each_object_its_result_where_falkordb_gives_a_date_id_back_as_its_text():
    gom = StatelessGraphObjectManager(Echoing())
    results = await gom.patch_all([PatchedDay(on=date(2024, 1, 3), value=1)], only=["value"])
    assert results == [PatchResult(True, True, True)]


# ----- An Oracle write block's id -----
#
# Each Oracle write block finds a node by its id as the shared module's hasId compares it, the property's class and key
# with the id's, which the index over the id is made over (drivine#754): the id bound once, in a row of its own, read
# EXTENDED, since the comparison reads it more than once and a dynamic statement's binds are by position. A list of ids,
# a fragment batch's or a save's stamped related nodes, or a trim's, is a row for each id, read EXTENDED, so a float is
# the binary double the node holds.

HINT = "/*+ NO_JSON_TABLE_TRANSFORM */"


class DialDays(GraphView):
    dial: Annotated[Dial, Root()]
    days: Annotated[list[StampedDay], Relationship("ON")] = []


def id_rows(listed: str) -> str:
    """Each id list's rows, bound as `listed`."""
    return f"JSON_TABLE(JSON({listed} EXTENDED), '$[*]' COLUMNS (v JSON PATH '$')) i"


def assert_match(match: str, key: str, labels: list[str]) -> None:
    """The bound match of a find_node or a write_node: the node of `labels` whose id `key` is the one bound `:1`."""
    carried = " AND ".join(
        f"INSTR(',' || LTRIM(RTRIM(JSON_SERIALIZE(f.labels), ']'), '[') || ',\"', ',\"{it}\",\"') > 0" for it in labels
    )
    assert match == (
        f"n.nid IN (SELECT {HINT} f.nid + 0 FROM {ONE_ID} CROSS JOIN drivine_node f WHERE {write_has_id(key)} "
        f"AND {carried})"
    )
    assert match.count(":1") == 1
    assert "PASSING" not in match


def matches(parameters: Any) -> list[str]:
    """The bound texts among `parameters` that are a find_node's or a write_node's match."""
    return [it for it in parameters.values() if isinstance(it, str) and ONE_ID in it]


def test_oracle_finds_a_patchs_a_patch_alls_a_relates_an_unrelates_and_an_unrelate_alls_node_by_has_id_bound_once():
    registry = ModelRegistry()
    backend = OracleBackend(OracleGraph())
    priced, dated = registry.fragment(Priced), registry.fragment(Dated)
    patched: Any = backend.patch_one(priced).spec({"id": "p", "set": {}, "vectors": {}}).parameters
    assert_match(patched["x_match"], "$$code", ["Priced"])
    many: Any = backend.patch_many(dated).spec({"rows": []}).parameters
    assert_match(many["x_match"], "on", ["Dated"])
    origin = MatchedNode(priced, "p", ())
    to = MatchedNode(dated, datetime(2024, 1, 2, tzinfo=UTC), ())
    for statement in (
        backend.relate(origin, to, "ON", {}, RelateMode.MERGE),
        backend.relate(origin, to, "ON", {}, RelateMode.CREATE),
        backend.unrelate(origin, to, "ON"),
    ):
        parameters: Any = statement.spec().parameters
        assert_match(parameters["x_frommatch"], "$$code", ["Priced"])
        assert_match(parameters["x_tomatch"], "on", ["Dated"])
        assert parameters["x_to"] == held_id(to.id)
    every: Any = backend.unrelate_all(origin, "ON", Direction.OUTGOING).spec().parameters
    assert_match(every["x_frommatch"], "$$code", ["Priced"])
    # Each of a node's labels is told by its text, as a lookup by id tells them.
    dial: Any = backend.patch_one(registry.fragment(Dial)).spec({"id": 1, "set": {}, "vectors": {}}).parameters
    assert_match(dial["x_match"], "id", ["Dial", "Meter"])


def test_oracle_finds_each_node_of_a_fragment_batch_by_has_id_and_reads_their_stamps_by_has_id_over_each_ids_row():
    model = ModelRegistry().fragment(StampedDay)
    days = [StampedDay(on=date(2024, 1, 2)), StampedDay(on=date(2024, 1, 3))]
    statement = OracleBackend(OracleGraph()).fragment_batch([FragmentGroup(model, days)], SaveOptions())
    parameters: Any = statement.spec().parameters
    found = matches(parameters)
    assert len(found) == 2
    for it in found:
        assert_match(it, "on", ["StampedDay"])
    # Read before and after the writes, each id of the list a row, which finds its node through the index.
    reads = (
        f"n.nid IN (SELECT {HINT} f.nid + 0 FROM {id_rows('$x_1')} CROSS JOIN drivine_node f WHERE {write_has_id('on')}"
    )
    assert reads in statement.text
    # The read itself reads each node's props beside the lookup's, so it is planned without the transformation too.
    assert statement.text.count(f"SELECT {HINT} NVL(JSON_ARRAYAGG(") == 2
    assert statement.text.count(f"CROSS JOIN drivine_node f WHERE {write_has_id('on')}") == 2
    assert parameters["x_1"] == held_id([it.on for it in days])
    assert "PASSING t.v" not in statement.text


def test_oracle_finds_a_saves_nodes_by_has_id_trims_a_replaced_list_by_has_id_over_each_id_kept_and_reads_stamps_so():
    registry = ModelRegistry()
    kept = StampedDay(on=date(2024, 1, 2))
    view = DialDays(dial=Dial(id=1), days=[kept])
    options = SaveOptions(replace=["days"], removed_targets=RemovedTargets.DELETE_UNREFERENCED)
    statement = OracleBackend(OracleGraph()).save(registry.view(DialDays), view, options, False, registry)
    parameters: Any = statement.spec().parameters
    found = matches(parameters)
    assert len(found) == 2
    assert_match(found[0], "id", ["Dial", "Meter"])
    assert_match(found[1], "on", ["StampedDay"])

    # The trim's two statements test each target joined to the root against the rows of the ids kept, bound :2, each
    # planned without Oracle's JSON table transformation, which lost a node's tests beside another table (spec#415).
    trims = [it for it in parameters.values() if isinstance(it, str) and write_has_id("on", "x") in it]
    assert len(trims) == 2
    for trim in trims:
        assert f"NOT EXISTS (SELECT {HINT} 1 FROM {id_rows(':2')} WHERE {write_has_id('on', 'x')})" in trim
        assert trim.count(":2") == 1
        assert trim.startswith(f"SELECT {HINT} ")
        assert "PASSING" not in trim
    # The stamps of the related days, read before and after, each id a row found through the index.
    reads = f"n.nid IN (SELECT {HINT} f.nid + 0 FROM {id_rows('$x_')}"
    assert re.sub(r"\$x_\d+", "$x_", statement.text).count(reads) == 2
    assert statement.text.count(f"SELECT {HINT} NVL(JSON_ARRAYAGG(") == 2
    assert statement.text.count(f"CROSS JOIN drivine_node f WHERE {write_has_id('on')}") == 2
    assert "PASSING t.v" not in statement.text
    # Each lookup hands its node back as `f.nid + 0`: Oracle eliminates a join of the node table to a view of it on a
    # bare nid, and the filter on the view with it (drivine-cypher-spec#160).
    for text in (statement.text, *found):
        assert not re.search(r"SELECT (/\*\+ [A-Z_ ]+ \*/ )?f\.nid FROM", text)
