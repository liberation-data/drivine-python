"""
`drivine_write` finds the node a step names by the step's id, the JSON the node holds it as, through the shared module's
hasId: Drivine for TypeScript's SqlitePlanWriter.spec.ts and drivine4j's `SqlitePlanWriter.find`. Found by the text of
its key, a date id saved twice was two nodes (#519), and a patch by a null id reached a node whose id is the text "null"
(#528).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from typing import Any

import apsw
import pytest

from drivine.annotation.direction import Direction
from drivine.backend.postgres.postgres_save_builder import stored_key
from drivine.backend.sql.node_writes import NodeWrites
from drivine.backend.sql.sql_json import to_json
from drivine.backend.sql.write_plan import WritePlan, WriteReturns
from drivine.backend.sqlite.sqlite_functions import register
from drivine.backend.sqlite.sqlite_graph import SqliteGraph

GRAPH = SqliteGraph()


@pytest.fixture
def db() -> Iterator[apsw.Connection]:
    connection = apsw.Connection(":memory:")
    register(connection)
    for statement in GRAPH.ddl:
        connection.execute(statement).close()
    yield connection
    connection.close()


def written(db: apsw.Connection, plan: WritePlan, returns: WriteReturns = "EACH_STAMP") -> list[Any]:
    """The plan's steps bound as a save binds them, each value in SQLite's layout: a date as its tag."""
    [(given,)] = db.execute(
        "SELECT drivine_write(?, ?, ?, ?, ?, ?)",
        (GRAPH.name, to_json(plan.plan), to_json(plan.shapes_bound), plan.mark, returns, None),
    ).fetchall()
    found: list[Any] = json.loads(given)
    return found


def names(db: apsw.Connection, label: str = "Day") -> list[str]:
    rows = db.execute(
        f"SELECT props ->> '$.name' FROM {GRAPH.nodes} WHERE instr(labels, ?) > 0 ORDER BY nid", (f'"{label}"',)
    ).fetchall()
    return [str(it[0]) for it in rows]


def saving(on: Any, name: str, plan: WritePlan | None = None) -> WritePlan:
    """A plan, `plan` or a new one, that saves the day `on`, named `name`."""
    plan = plan or WritePlan("sqlite")
    plan.node(plan.shape(["Day"], "on"), "on", on, stored_key(on, "sqlite"), ["Day"], NodeWrites(set={"name": name}))
    return plan


def test_a_date_id_saved_twice_is_one_node(db: apsw.Connection):
    day = date(2024, 1, 2)
    written(db, saving(day, "first"))
    written(db, saving(date(2024, 1, 2), "second"))
    assert names(db) == ["second"]
    # Twice in one plan, as a save_all writes it, is one node too.
    later = date(2024, 1, 3)
    written(db, saving(later, "fourth", saving(later, "third")))
    assert names(db) == ["second", "fourth"]


def test_a_ref_and_a_patch_find_their_node_by_the_ids_json_and_a_null_id_finds_none(db: apsw.Connection):
    day = date(2024, 1, 2)
    written(db, saving(day, "first"))
    patching = WritePlan("sqlite")
    patching.patch(patching.shape(["Day"], "on"), stored_key(day, "sqlite"), day, "0", {"name": "patched"}, [])
    assert [str(it).split("=")[0] for it in written(db, patching)] == ["0/1"]
    assert names(db) == ["patched"]
    # Beside a node whose id is the text "null", which a patch by a null id once reached (#528).
    written(db, saving("null", "text"))
    nobody = WritePlan("sqlite")
    nobody.patch(nobody.shape(["Day"], "on"), stored_key(None, "sqlite"), None, "0", {"name": "none"}, [])
    assert written(db, nobody) == []
    assert names(db) == ["patched", "text"]
    joined = WritePlan("sqlite")
    shape = joined.shape(["Day"], "on")
    joined.link(
        joined.ref(shape, stored_key(day, "sqlite"), day),
        joined.ref(shape, stored_key(day, "sqlite"), day),
        "NEXT",
        Direction.OUTGOING,
        {},
    )
    assert written(db, joined, "COUNT") == [1]


def test_a_target_of_a_date_id_is_unlinked_by_its_tags_text_and_the_one_still_held_kept(db: apsw.Connection):
    first, second = date(2025, 1, 1), date(2025, 1, 2)

    def rooted() -> tuple[WritePlan, int]:
        plan = WritePlan("sqlite")
        root = plan.node(plan.shape(["Owner"], "id"), "id", "o1", "o1", ["Owner"], NodeWrites(set={"name": "o"}))
        return plan, root

    plan, root = rooted()
    for on in (first, second):
        day = plan.node(
            plan.shape(["Day"], "on"), "on", on, stored_key(on, "sqlite"), ["Day"], NodeWrites({"name": str(on)})
        )
        plan.link(root, day, "ON_DAY", Direction.OUTGOING, {})
    written(db, plan)

    def stamp_of(name: str) -> Any:
        return db.execute(f"SELECT stamp FROM {GRAPH.nodes} WHERE props ->> '$.name' = ?", (name,)).fetchall()

    held = stamp_of(str(second))
    trim, root = rooted()
    kept = stored_key(second, "sqlite")
    assert kept == "2025-01-02"
    trim.unlink(root, "ON_DAY", Direction.OUTGOING, trim.shape(["Day"], "on"), [kept], True, False, [kept])
    written(db, trim)
    edges = db.execute(
        f"SELECT e.type, t.props ->> '$.name' FROM {GRAPH.edges} e JOIN {GRAPH.nodes} t ON t.nid = e.dst"
    ).fetchall()
    assert [tuple(it) for it in edges] == [("ON_DAY", str(second))]
    assert stamp_of(str(second)) == held
    assert sorted(names(db)) == [str(first), str(second)]
