# Written by scripts/unasync.py from tests/integration/sqlite/migration/test_sqlite_store_layout_migration.py:
# change that, and run the script again.
"""
A SQLite graph drivine-python 0.8.0 wrote, in store layout 1 (`layout1-0.8.0.sqlite`, beside this test, written by
`scripts/write_migration_fixture_0_8_0.py`), migrated to layout 2 by the shared module's migrator, run by
StoreLayoutMigration on the store itself (drivine-python#514, drivine-cypher-spec#256): refused before, a dry run that
writes nothing, a run cut short after its first written batch and resumed, two spellings of one moment under a unique
index stopping it as `collisions`, resumed once corrected with plain SQL, and read by this version as layout 2 after,
its models loaded. drivine4j's StoreLayoutMigrationSqliteTest and Drivine for TypeScript's StoreLayoutMigration.spec.ts,
ported.

The migrator is in no released compiler yet: the test runs where the compiler installed answers a `migrate` request
(drivine-cypher-spec#344, #348 and #350, released after 0.50.0), and is skipped elsewhere.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any

import apsw
import pytest

from drivine.query.statement import cypher_statement
from drivine.sync import (
    DatabaseRegistry,
    DatabaseType,
    GraphView,
    NodeFragment,
    NodeId,
    PersistenceManager,
    PersistenceManagerFactory,
    QuerySpecification,
    Relationship,
    RelationshipFragment,
    Root,
    StatelessGraphObjectManager,
    StoreLayoutError,
    TargetNode,
    refs,
)
from drivine.sync.connection.sqlite.sqlite_connection_provider import SqliteConnectionProvider
from drivine.sync.migration.store_layout_migration import (
    MigrationDeclaration,
    StoreLayoutMigration,
    declarations_of,
    with_declared,
)

FIXTURE = Path(__file__).parents[3] / "integration" / "sqlite" / "migration" / "layout1-0.8.0.sqlite"
"""The fixture, which the sync twin of this test reads too."""


def _migrator() -> Callable[[str], str] | None:
    """The installed compiler's JSON entry, where it answers a `migrate` request; None where it doesn't."""
    try:
        from drivine_cypher_compiler import compile as compile_json
    except ModuleNotFoundError:
        return None
    answer = json.loads(compile_json(json.dumps({"migrate": {"store": "sqlite", "graph": "drivine", "start": {}}})))
    return compile_json if "run" in answer or "done" in answer else None


pytestmark = pytest.mark.skipif(
    _migrator() is None,
    reason="the installed drivine-cypher-compiler has no store layout migrator (drivine-cypher-spec#344, #348, #350): "
    "it needs the compiler released after 0.50.0",
)


class Person(NodeFragment, labels=["Person"]):
    name: Annotated[str, NodeId()]
    born: date | None = None
    height: float | None = None
    visits: int | None = None
    nick: str | None = None


class Meeting(NodeFragment, labels=["Meeting"]):
    id: Annotated[str, NodeId()]
    at: datetime


class File(NodeFragment, labels=["File"]):
    name: Annotated[str, NodeId()]
    data: bytes


class Knows(RelationshipFragment):
    since: date
    target: Annotated[Person, TargetNode()]


class Circle(GraphView):
    person: Annotated[Person, Root()]
    knows: Annotated[list[Knows], Relationship("KNOWS")] = []


DECLARATIONS = with_declared(
    [
        *declarations_of(Person),
        *declarations_of(Meeting),
        *declarations_of(File),
        *declarations_of(Knows, "KNOWS"),
    ],
    # The Events were written in Cypher, and have no model to say what they hold.
    [MigrationDeclaration("Event", "at", "datetime")],
)


class Interrupted(Exception):
    """The process stopped, as far as the migration knows: between a round's outcomes and the module's next answer."""


def interrupted_after_a_write(compile_json: Callable[[str], str]) -> Callable[[str], str]:
    """
    The module's entry, which stops the run as the outcomes of its first round that rewrote rows are handed back.
    """
    wrote = False

    def asked(request: str) -> str:
        nonlocal wrote
        if wrote:
            raise Interrupted
        answer = compile_json(request)
        wrote = any(
            statement.get("purpose") == "rewrite"
            for action in json.loads(answer).get("run", [])
            for statement in action.get("statements", [])
        )
        return answer

    return asked


def snapshot(path: str) -> list[str]:
    """Every object of the file and every row of the graph, as text: what a dry run must leave as it was."""
    connection = apsw.Connection(path)
    try:
        return [
            f"{a}|{b}"
            for a, b in connection.execute(
                "SELECT name, sql FROM sqlite_master UNION ALL SELECT nid, props FROM drivine_node "
                "UNION ALL SELECT eid, props FROM drivine_edge ORDER BY 1, 2"
            )
        ]
    finally:
        connection.close()


def sql(path: str, statement: str) -> list[Any]:
    connection = apsw.Connection(path)
    try:
        return list(connection.execute(statement))
    finally:
        connection.close()


@pytest.fixture
def path(tmp_path: Path) -> str:
    copied = tmp_path / "layout1.sqlite"
    shutil.copyfile(FIXTURE, copied)
    return str(copied)


def refused(path: str) -> None:
    """That a first connection refuses the graph, as of a layout this version doesn't read."""
    provider = SqliteConnectionProvider("refused", path)
    try:
        with pytest.raises(StoreLayoutError):
            provider.connect()
    finally:
        provider.end()


def one(pm: PersistenceManager, statement: str) -> Any:
    [found] = pm.query(QuerySpecification[Any](cypher_statement(statement)))
    return found


def test_a_graph_0_8_0_wrote_is_refused_dry_run_resumed_stopped_at_a_collision_corrected_and_read_as_layout_2(
    path: str,
):
    refused(path)

    provider = SqliteConnectionProvider("migration", path)
    with pytest.raises(Exception, match="not yet proven"):
        StoreLayoutMigration(provider).migrate(DECLARATIONS)

    untouched = snapshot(path)
    dry_run = StoreLayoutMigration(provider).migrate(DECLARATIONS, dry_run=True, unproven=True)
    assert dry_run["status"] == "dryRun", dry_run["message"]
    assert snapshot(path) == untouched, "a dry run writes nothing"
    assert len(dry_run["notes"]) == 2
    assert dry_run["tables"] == {"nodes": {"rows": 10, "changed": 7}, "edges": {"rows": 1, "changed": 1}}
    assert {"label": "File", "property": "data", "changes": {"bytes": 1}, "invalid": []} in dry_run["properties"]
    assert {"label": "Robot", "property": "$t", "changes": {"escapedKey": 1}, "invalid": []} in dry_run["properties"]
    # Person.nick holds a text that reads as a date, in its one row: a candidate, never applied.
    assert {"label": "Person", "property": "nick", "kind": "date", "values": 1} in dry_run["candidates"]
    assert all(not it["invalid"] for it in dry_run["properties"]), dry_run["properties"]

    # Stopped after its first batch of writes, two rows a batch, then begun again: it resumes after that batch.
    with pytest.raises(Interrupted):
        StoreLayoutMigration(provider, interrupted_after_a_write(_compile())).migrate(
            DECLARATIONS, batch_size=2, unproven=True
        )
    assert sql(path, "SELECT count(*) FROM sqlite_master WHERE name = 'drivine_migrate2'") == [(1,)]
    # The first batch rewritten, Ada's and Charles's nodes, their dates tagged; the rows after it not yet.
    born = "SELECT json_type(props, '$.born') FROM drivine_node WHERE json_extract(props, '$.name') = 'Ada'"
    at = "SELECT json_type(props, '$.at') FROM drivine_node WHERE json_extract(props, '$.id') = 'm1'"
    assert (sql(path, born), sql(path, at)) == ([("object",)], [("text",)])
    refused(path)

    # Two spellings of one moment are one value in layout 2: the run stops, every value rewritten, the graph refused.
    stopped = StoreLayoutMigration(provider).migrate(DECLARATIONS, batch_size=2, unproven=True)
    assert stopped["status"] == "collisions", stopped["message"]
    assert [(it["label"], it["properties"]) for it in stopped["collisions"]] == [("Event", ["at"])]
    refused(path)

    # Corrected with the store's own tools, then resumed: migrated.
    [(event,)] = sql(path, "SELECT max(nid) FROM drivine_node WHERE labels LIKE '%\"Event\"%'")
    sql(path, f"DELETE FROM drivine_node WHERE nid = {event}")
    migrated = StoreLayoutMigration(provider).migrate(DECLARATIONS, unproven=True)
    assert migrated["status"] == "migrated", migrated["message"]
    # A migrated graph is current: the next run has nothing to do.
    again = StoreLayoutMigration(provider).migrate(DECLARATIONS, unproven=True)
    assert again["status"] == "current", again["message"]
    provider.end()

    # This version reads it as layout 2: an ordinary graph of it, its models loaded with their values' kinds.
    registry = DatabaseRegistry()
    registry.builder().database_type(DatabaseType.SQLITE).database_name(path).register("migrated")
    try:
        pm = PersistenceManagerFactory(registry).get("migrated")
        gom = StatelessGraphObjectManager(pm)
        ada = gom.load(Person, "Ada")
        assert ada == Person(name="Ada", born=date(1815, 12, 10), height=1.65, visits=3)
        charles = gom.load(Person, "Charles")
        assert charles is not None and isinstance(charles.height, float) and charles.height == 2.0
        assert one(pm, "MATCH (p:Person) WHERE p.born < date('1800-01-01') RETURN p.name") == "Charles"
        assert one(pm, "MATCH (p:Person {name: 'Charles'}) RETURN toString(p.height)") == "2.0"
        circle = gom.load(Circle, "Bob")
        assert circle is not None
        assert [(it.since, it.target.name) for it in circle.knows] == [(date(2020, 1, 1), "Cy")]
        assert one(pm, "MATCH ()-[k:KNOWS]->() RETURN k.since = date('2020-01-01')") is True
        assert gom.load(Meeting, "m1") == Meeting(id="m1", at=datetime(2024, 3, 15, 10, 30, 0, 500000, tzinfo=UTC))
        assert gom.load(File, "f1") == File(name="f1", data=b"\x00\x01\xfe\xff")
        assert one(pm, "MATCH (r:Robot) RETURN r.`$t`") == 1
        assert one(pm, "MATCH (r:Robot) RETURN r.`$x`") == "y"
        # An undeclared text that reads as a date stays a text.
        mary = gom.load(Person, "Mary")
        assert mary is not None and mary.nick == "2024-03-15"
        assert one(pm, "MATCH (p:Person {name: 'Mary'}) RETURN p.nick = date('2024-03-15')") is False
        born = gom.load_all(Person, where=refs(Person).born.is_not_null())
        assert sorted(it.name for it in born) == ["Ada", "Charles"]
        # Saved and loaded again as any layout-2 graph's node is.
        gom.save(Person(name="Grace", born=date(1906, 12, 9)))
        assert one(pm, "MATCH (p:Person) WHERE p.born > date('1900-01-01') RETURN p.name") == "Grace"
    finally:
        registry.close()
    # Layout 1's indexes are dropped; the migration made none, and left no checkpoint.
    objects = [it.split("|")[0] for it in snapshot(path) if it.startswith("drivine_")]
    assert not [it for it in objects if it.startswith("drivine_uq_") or it == "drivine_migrate2"], objects


def _compile() -> Callable[[str], str]:
    found = _migrator()
    assert found is not None
    return found
