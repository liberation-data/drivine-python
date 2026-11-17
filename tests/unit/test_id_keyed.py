"""
The id a load, a delete, a cascade's check, a save's reads and the relationship statements bind, on each SQL store, as
the store holds the node's id (Drivine for TypeScript's IdKeyed.spec.ts): on Postgres and SQLite the text the store
reads the property back as (stored_key), and on Oracle extended JSON's double, read `JSON($id EXTENDED)` (held_id).
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Any, TypeVar

import apsw
import pytest

from drivine import (
    CascadeType,
    DatabaseType,
    Direction,
    GraphView,
    NodeFragment,
    NodeId,
    QuerySpecification,
    RelateMode,
    Relationship,
    Root,
    StatelessGraphObjectManager,
    node_ref,
)
from drivine.backend.graph_backend import GraphBackend
from drivine.backend.matched_node import MatchedNode
from drivine.backend.oracle.oracle_backend import OracleBackend
from drivine.backend.oracle.oracle_graph import OracleGraph
from drivine.backend.postgres.postgres_backend import PostgresBackend
from drivine.backend.postgres.postgres_graph import PostgresGraph
from drivine.backend.postgres.postgres_save_builder import numeric_text, stored_key
from drivine.backend.sql.sql_json import to_json
from drivine.backend.sqlite.sqlite_backend import SqliteBackend
from drivine.backend.sqlite.sqlite_graph import SqliteGraph, text_of
from drivine.cursor.cursor import Cursor
from drivine.cursor.cursor_specification import CursorSpecification
from drivine.manager.persistence_manager import PersistenceManager
from drivine.model.model_registry import ModelRegistry

T = TypeVar("T")


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
    (DatabaseType.POSTGRES, lambda: PostgresBackend(PostgresGraph()), "0.00000010"),
    (DatabaseType.SQLITE, lambda: SqliteBackend(SqliteGraph()), "1.0E-7"),
]
"""
Postgres and SQLite, and the text each reads the property of a node with the id 1e-7 back as: Python's text of it is
`1e-07`, which no node holds.
"""


@pytest.mark.parametrize(("type_", "backend", "key"), SQL_KEYED)
async def test_a_float_id_is_bound_by_a_load_a_delete_and_a_cascade_as_the_text_the_store_holds_it_as(
    type_: DatabaseType, backend: Any, key: str
):
    gom, pm = manager(type_)
    await loaded_and_deleted(gom)
    # The cascade's check of its depth, and the cascade itself.
    assert [it["id"] for _, it in pm.bound] == [key] * 5


@pytest.mark.parametrize(("type_", "backend", "key"), SQL_KEYED)
def test_a_float_id_is_bound_by_a_saves_reads_of_the_nodes_properties_and_stamp_as_the_text_the_store_holds_it_as(
    type_: DatabaseType, backend: Any, key: str
):
    store: GraphBackend = backend()
    model = ModelRegistry().fragment(Gauge)
    held = store.held_properties(model).spec({"id": 1e-7}).parameters
    found = store.found_stamp(model).spec({"id": 1e-7}).parameters
    assert [held["id"], found["id"]] == [key, key]  # type: ignore[index]


@pytest.mark.parametrize(("type_", "backend", "key"), SQL_KEYED)
async def test_a_float_id_is_bound_by_load_related_as_the_text_the_store_holds_it_as(
    type_: DatabaseType, backend: Any, key: str
):
    gom, pm = manager(type_)
    await gom.edges.load_related(node_ref(Gauge, 1e-7), "POINTS", Gauge)
    assert [it["x_from"] for _, it in pm.bound] == [key]


@pytest.mark.parametrize(("type_", "backend", "key"), SQL_KEYED)
async def test_a_text_id_and_a_null_id_are_bound_as_they_are(type_: DatabaseType, backend: Any, key: str):
    gom, pm = manager(type_)
    await gom.load(Gauge, "1e-7")
    await gom.load(Gauge, None)
    assert [it["id"] for _, it in pm.bound] == ["1e-7", None]


ORACLE_HELD = '{"$numberDouble":"1.0E-7"}'
"""The id 1e-7 as Oracle's layout holds it, a binary double: extended JSON's double."""

READ_EXTENDED = re.compile(r'PASSING JSON\(\S+ EXTENDED\) AS "id__"')


async def test_a_float_id_on_oracle_is_bound_by_a_load_a_delete_and_a_cascade_as_the_json_oracle_holds_it_as():
    # A NUMBER bound as it is, 1e-7, is not always equal to the binary double the node holds.
    gom, pm = manager(DatabaseType.ORACLE)
    await loaded_and_deleted(gom)
    assert [it["id"] for _, it in pm.bound] == [ORACLE_HELD] * 5
    assert all(READ_EXTENDED.search(text) for text, _ in pm.bound)


def test_oracle_binds_a_saves_reads_of_the_nodes_properties_and_stamp_as_the_json_oracle_holds_the_id_as():
    backend = OracleBackend(OracleGraph())
    model = ModelRegistry().fragment(Gauge)
    for statement in (backend.held_properties(model), backend.found_stamp(model)):
        assert READ_EXTENDED.search(statement.text)
        assert statement.spec({"id": 1e-7}).parameters["id"] == ORACLE_HELD  # type: ignore[index]


def test_oracle_binds_the_ends_of_load_related_unrelate_all_and_relate_as_the_json_oracle_holds_the_id_as():
    backend = OracleBackend(OracleGraph())
    model = ModelRegistry().fragment(Gauge)
    origin = MatchedNode(model, 1e-7, ())
    related = backend.related(origin, "POINTS", Direction.OUTGOING, model)
    assert 'PASSING JSON($x_from EXTENDED) AS "v"' in related.text
    assert related.spec().parameters["x_from"] == ORACLE_HELD  # type: ignore[index]
    unrelated = backend.unrelate_all(origin, "POINTS", Direction.OUTGOING).spec().parameters
    assert unrelated["x_from"] == ORACLE_HELD  # type: ignore[index]
    relate: Any = backend.relate(origin, MatchedNode(model, 4.5, ()), "POINTS", {}, RelateMode.MERGE).spec().parameters
    assert [relate["x_from"], relate["x_to"]] == [ORACLE_HELD, '{"$numberDouble":"4.5"}']
    assert 'PASSING JSON(:1 EXTENDED) AS "v"' in relate["x_frommatch"]


async def test_oracle_binds_a_text_id_as_its_json_and_a_null_id_as_it_is():
    gom, pm = manager(DatabaseType.ORACLE)
    await gom.load(Gauge, "1e-7")
    await gom.load(Gauge, 7)
    await gom.load(Gauge, None)
    assert [it["id"] for _, it in pm.bound] == ['"1e-7"', "7", None]


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
