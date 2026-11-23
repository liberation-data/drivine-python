# Written by scripts/unasync.py from tests/integration/migration/test_store_layout_migration_servers.py:
# change that, and run the script again.
"""
A graph drivine-python 0.8.0 wrote, in store layout 1, into the test's own Postgres or Oracle, migrated to layout 2
by the shared module's migrator as StoreLayoutMigration runs it on the store (drivine-cypher-spec#256,
drivine-python#514): what test_sqlite_store_layout_migration shows on a file, shown on a server. drivine4j's
StoreLayoutMigrationStoreTest, ported.

The graph is written by 0.8.0 itself (scripts/write_migration_fixture_0_8_0.py, run by `uv` in an environment of its
own), so it is the layout 1 a release wrote, not this checkout's idea of it. Each test has graphs of its own.

- A dry run writes nothing and reports what a run would do.
- A run stopped by two spellings of one moment under a unique key is corrected with SQL and resumed, and the graph it
  leaves is the graph this version writes from the same models and statements: the same rows, the same stamp and
  marker, and, once the application has made its schema, the same indexes. The internal ids, and the count store the
  migration leaves unseeded (drivine-cypher-spec#349), are the differences, and the only ones.
- A run stopped by a failed batch, or by its session killed while it writes, is resumed to the end, each row rewritten
  once.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from drivine.query.statement import cypher_statement
from drivine.sync import (
    PersistenceManagerFactory,
    QuerySpecification,
    StatelessGraphObjectManager,
    StoreLayoutError,
    uniqueness_constraint,
)
from drivine.sync.migration.store_layout_migration import Declaration, MigrationReport, StoreLayoutMigration
from tests.sync_integration.conftest import ORACLE, POSTGRES
from tests.sync_integration.migration.models import DECLARATIONS, Circle, File, Knows, Meeting, Person
from tests.sync_integration.sql.new_graph import registry_on

WRITER = Path(__file__).resolve().parents[3] / "scripts" / "write_migration_fixture_0_8_0.py"

SERVERS = [*(["postgres"] if POSTGRES else []), *(["oracle"] if ORACLE else [])]
"""The servers configured: Postgres as the tests' own environment configures it, and Oracle with ORACLE_TESTS=true."""

pytestmark = pytest.mark.skipif(not SERVERS, reason="Neither Postgres nor Oracle is configured.")


def env(store: str, name: str) -> str:
    return os.environ[f"{store.upper()}_DATABASE_{name}"]


class Server:
    """What a test asks of the store in its own SQL, on a connection of the driver's own, outside Drivine."""

    def __init__(self, store: str) -> None:
        self.store = store

    @contextmanager
    def connection(self, user: str | None = None, password: str | None = None) -> Iterator[Any]:
        user, password = user or env(self.store, "USER"), password or env(self.store, "PASSWORD")
        host, port, database = env(self.store, "HOST"), env(self.store, "PORT"), env(self.store, "NAME")
        if self.store == "postgres":
            import psycopg

            with psycopg.connect(
                host=host, port=int(port), user=user, password=password, dbname=database, autocommit=True
            ) as connection:
                yield connection
        else:
            import oracledb

            connection = oracledb.connect(user=user, password=password, dsn=f"{host}:{port}/{database}")
            connection.autocommit = True

            def lobs_as_values(cursor: Any, metadata: Any) -> Any:
                # A CLOB as its text, a BLOB as its bytes: rows compared, not streamed.
                if metadata.type_code is oracledb.DB_TYPE_CLOB:
                    return cursor.var(oracledb.DB_TYPE_LONG, arraysize=cursor.arraysize)
                if metadata.type_code is oracledb.DB_TYPE_BLOB:
                    return cursor.var(oracledb.DB_TYPE_LONG_RAW, arraysize=cursor.arraysize)
                return None

            connection.outputtypehandler = lobs_as_values
            try:
                yield connection
            finally:
                connection.close()

    def execute(self, statement: str) -> None:
        with self.connection() as connection:
            connection.cursor().execute(statement)

    def rows(self, query: str) -> list[tuple[Any, ...]]:
        with self.connection() as connection:
            cursor = connection.cursor()
            cursor.execute(query)
            return [tuple(it) for it in cursor.fetchall()]

    def texts(self, query: str) -> list[str]:
        """Each row of the query, its columns as text joined by `|`."""
        return ["|".join("" if it is None else str(it) for it in row) for row in self.rows(query)]

    def json(self, column: str) -> str:
        """A SQL expression that writes the JSON column as text that tells each value's type."""
        # Extended on Oracle, so that a binary double is told from a NUMBER, as the migration reads them.
        return f"{column}::text" if self.store == "postgres" else f"JSON_SERIALIZE({column} RETURNING CLOB EXTENDED)"

    def tables(self, graph: str) -> list[str]:
        """The graph's own tables, by their names in lower case."""
        if self.store == "postgres":
            query = (
                "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() "
                f"AND table_name LIKE '{graph}\\_%'"
            )
        else:
            query = f"SELECT table_name FROM user_tables WHERE table_name LIKE UPPER('{graph}\\_%') ESCAPE '\\'"
        return sorted(it.lower() for it in self.texts(query))

    def table_rows(self, table: str) -> list[dict[str, Any]]:
        """Each row of the table, as its columns' values, a JSON column's value read as JSON."""
        if self.store == "postgres":
            return [json.loads(it) for it in self.texts(f"SELECT row_to_json(t)::text FROM {table} t")]
        columns = self.rows(
            f"SELECT LOWER(column_name), data_type FROM user_tab_columns WHERE table_name = UPPER('{table}') "
            "ORDER BY column_id"
        )
        read = ", ".join(self.json(name) if kind == "JSON" else name for name, kind in columns)
        return [
            {
                name: json.loads(value) if kind == "JSON" and value is not None else value
                for (name, kind), value in zip(columns, row, strict=True)
            }
            for row in self.rows(f"SELECT {read} FROM {table}")
        ]

    def indexes(self, graph: str) -> list[str]:
        """The indexes on the graph's tables, one a row: its name and what it indexes, as the store writes them."""
        if self.store == "postgres":
            return self.texts(
                "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema() "
                f"AND tablename LIKE '{graph}\\_%'"
            )
        # The indexes Oracle names itself (a primary key's, a LOB's) are named apart in each graph, and left out. An
        # index built over rows is written COMPUTE STATISTICS, one built on an empty table not: how it was built, not
        # what it indexes.
        return self.texts(
            "SELECT index_name, REPLACE(DBMS_METADATA.GET_DDL('INDEX', index_name), ' COMPUTE STATISTICS') "
            f"FROM user_indexes WHERE table_name LIKE UPPER('{graph}\\_%') ESCAPE '\\' "
            "AND index_name NOT LIKE 'SYS\\_%' ESCAPE '\\'"
        )

    def property_is(self, prop: str, text: str) -> str:
        """Where the property of the row `n` of a node table is the text."""
        if self.store == "postgres":
            return f"n.props->>'{prop}' = '{text}'"
        return f"JSON_VALUE(n.props, '$.{prop}') = '{text}'"

    def labelled(self, label: str) -> str:
        """Where the row `n` of a node table has the label."""
        if self.store == "postgres":
            return f"n.labels @> '[\"{label}\"]'"
        return f"JSON_EXISTS(n.labels, '$?(@ == \"{label}\")')"

    def stop_at_robot(self, graph: str, sleep: bool) -> None:
        """Makes the write of the Robot's row fail with `stopped by the test`, or, with `sleep`, wait."""
        if self.store == "postgres":
            stop = "PERFORM pg_sleep(600);" if sleep else "RAISE EXCEPTION 'stopped by the test';"
            self.execute(
                f"CREATE FUNCTION {graph}_test_stop() RETURNS trigger LANGUAGE plpgsql AS $$ "
                f"BEGIN IF OLD.props->>'name' = 'R2' THEN {stop} END IF; RETURN NEW; END $$"
            )
            self.execute(
                f"CREATE TRIGGER {graph}_test_stop BEFORE UPDATE ON {graph}_node FOR EACH ROW "
                f"EXECUTE FUNCTION {graph}_test_stop()"
            )
        else:
            # The sleeper names its graph as its action, for kill_sleeper to kill it alone.
            stop = (
                f"DBMS_APPLICATION_INFO.SET_ACTION('{graph}'); DBMS_SESSION.SLEEP(600);"
                if sleep
                else "RAISE_APPLICATION_ERROR(-20001, 'stopped by the test');"
            )
            self.execute(
                f"CREATE OR REPLACE TRIGGER {graph}_test_stop BEFORE UPDATE ON {graph}_node FOR EACH ROW\n"
                "DECLARE\n    v_name VARCHAR2(400);\nBEGIN\n"
                "    SELECT JSON_VALUE(:OLD.props, '$.name') INTO v_name FROM dual;\n"
                f"    IF v_name = 'R2' THEN {stop} END IF;\nEND;"
            )

    def unstop(self, graph: str) -> None:
        """Removes what stop_at_robot made."""
        if self.store == "postgres":
            self.execute(f"DROP TRIGGER IF EXISTS {graph}_test_stop ON {graph}_node")
            self.execute(f"DROP FUNCTION IF EXISTS {graph}_test_stop()")
        else:
            self.execute(f"DROP TRIGGER IF EXISTS {graph}_test_stop")

    def kill_sleeper(self, graph: str) -> bool:
        """
        Kills the session waiting in stop_at_robot's sleep in the graph, once there is one: true when it found one to
        kill. Only that one: the store may be a local instance others use, and Oracle's schema DDL sleeps too while it
        waits.
        """
        if self.store == "postgres":
            # The sleeping session's statement is the migration's update of the graph's nodes, which names the graph.
            return any(
                killed
                for (killed,) in self.rows(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE wait_event = 'PgSleep' "
                    f"AND pid <> pg_backend_pid() AND query LIKE '%{graph}%'"
                )
            )
        import oracledb

        # As system, as the test user may not see or kill another session.
        with self.connection("system", env(self.store, "PASSWORD")) as admin:
            cursor = admin.cursor()
            cursor.execute(
                "SELECT sid || ',' || serial# FROM v$session WHERE username = UPPER(:owner) "
                "AND event = 'PL/SQL lock timer' AND action = :graph",
                owner=env(self.store, "USER"),
                graph=graph,
            )
            sessions = [it for (it,) in cursor.fetchall()]
            for session in sessions:
                try:
                    cursor.execute(f"ALTER SYSTEM KILL SESSION '{session}' IMMEDIATE")
                except oracledb.DatabaseError as error:
                    # ORA-00031: the session is marked to be killed, as it will be once its call returns: killed all
                    # the same.
                    if error.args[0].code != 31:
                        raise
            return bool(sessions)


@pytest.fixture(params=SERVERS)
def server(request: pytest.FixtureRequest) -> Server:
    return Server(request.param)


def graph_name(name: str) -> str:
    """A graph name of the test's own, unused on a store kept between runs."""
    return f"mig_{name}_{uuid.uuid4().hex[:8]}"


def write_layout1(server: Server, graph: str) -> None:
    """Writes the fixture's layout-1 graph with drivine-python 0.8.0, in an environment and a process of its own."""
    uv = shutil.which("uv")
    assert uv is not None, "0.8.0's writer is run by uv, which isn't on the PATH"
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    store = server.store
    arguments = [env(store, it) for it in ("HOST", "PORT", "USER", "PASSWORD", "NAME")]
    # From an empty directory, with -I, so that this checkout's drivine isn't the one imported; its output to a file,
    # not a pipe read to its end, which would wait on a writer that hangs past the timeout.
    with tempfile.TemporaryDirectory() as empty, tempfile.TemporaryFile("w+") as output:
        command = [uv, "run", "--no-project", "--python", python, "--with", f"drivine[{store}]==0.8.0", "python", "-I"]
        try:
            process = subprocess.run(
                [*command, str(WRITER), store, *arguments, graph],
                cwd=empty,
                stdout=output,
                stderr=subprocess.STDOUT,
                timeout=300,
                check=False,
                env={k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT")},
            )
        except subprocess.TimeoutExpired:
            output.seek(0)
            pytest.fail(f"0.8.0's writer didn't finish in 5 minutes: {output.read()}")
        output.seek(0)
        assert process.returncode == 0, f"0.8.0's writer: {output.read()}"


def migrate(
    server: Server, graph: str, declarations: list[Declaration] = DECLARATIONS, **options: Any
) -> MigrationReport:
    """Runs a migration of the graph on a provider of its own, ended after."""
    registry = registry_on(server.store, graph)
    try:
        provider = registry.registered(server.store)
        assert provider is not None
        return StoreLayoutMigration(provider).migrate(declarations, unproven=True, **options)  # pyright: ignore[reportArgumentType]
    finally:
        registry.close()


def refused(server: Server, graph: str) -> None:
    """That a first connection refuses the graph, as of a layout this version doesn't read."""
    registry = registry_on(server.store, graph)
    try:
        provider = registry.registered(server.store)
        assert provider is not None
        with pytest.raises(StoreLayoutError):
            provider.connect()
    finally:
        registry.close()


def snapshot(server: Server, graph: str) -> list[str]:
    """Every row of every table of the graph, and its indexes: what a dry run must leave as it was."""
    rows = [
        f"{table}: {json.dumps(row, sort_keys=True, default=str)}"
        for table in server.tables(graph)
        for row in server.table_rows(table)
    ]
    return sorted(rows) + sorted(server.indexes(graph))


def props(server: Server, graph: str, where: str) -> str:
    [found] = server.texts(f"SELECT {server.json('n.props')} FROM {graph}_node n WHERE {where}")
    return found


def robot(server: Server) -> str:
    return server.property_is("name", "R2")


def later_event(server: Server, graph: str) -> str:
    """
    The id of the Event 0.8.0 wrote second, which collides with the first in layout 2: read before its text is
    rewritten.
    """
    [found] = server.texts(
        f"SELECT n.nid FROM {graph}_node n WHERE {server.labelled('Event')} "
        f"AND {server.property_is('at', '2024-03-15T10:30:00.500Z')}"
    )
    return found


def test_a_graph_0_8_0_wrote_is_refused_dry_run_stopped_at_a_collision_corrected_and_migrated_as_written(
    server: Server,
):
    graph = graph_name("full")
    write_layout1(server, graph)
    refused(server, graph)
    later = later_event(server, graph)

    untouched = snapshot(server, graph)
    dry_run = migrate(server, graph, dry_run=True)
    assert dry_run["status"] == "dryRun", dry_run["message"]
    assert snapshot(server, graph) == untouched, "a dry run writes nothing"
    assert {"label": "Person", "property": "nick", "kind": "date", "values": 1} in dry_run["candidates"]
    assert dry_run["tables"]["nodes"]["rows"] > 0 and dry_run["tables"]["nodes"]["changed"] > 0, dry_run["tables"]
    born = [it for it in dry_run["properties"] if (it["label"], it["property"]) == ("Person", "born")]
    assert born and born[0]["changes"].get("temporal", 0) > 0, dry_run["properties"]
    assert all(not it["invalid"] for it in dry_run["properties"]), dry_run["properties"]

    stopped = migrate(server, graph)
    assert stopped["status"] == "collisions", stopped["message"]
    assert [(it["label"], it["properties"]) for it in stopped["collisions"]] == [("Event", ["at"])]
    refused(server, graph)

    server.execute(f"DELETE FROM {graph}_node WHERE nid = {later}")
    migrated = migrate(server, graph)
    assert migrated["status"] == "migrated", migrated["message"]
    again = migrate(server, graph)
    assert again["status"] == "current", again["message"]
    assert not [it for it in server.tables(graph) if it.endswith("_migrate2")], "no checkpoint is left"

    reads_as_layout_2(server, graph)
    same_as_written(server, graph)


def test_a_run_stopped_by_a_failed_batch_is_reported_failed_its_committed_batches_kept_and_resumed_to_the_end(
    server: Server,
):
    graph = graph_name("failed")
    write_layout1(server, graph)
    ada = server.property_is("name", "Ada")
    ada_before = props(server, graph, ada)
    robot_before = props(server, graph, robot(server))
    later = later_event(server, graph)
    server.stop_at_robot(graph, sleep=False)
    failed = migrate(server, graph, batch_size=2)
    assert failed["status"] == "failed", failed["message"]
    assert "stopped by the test" in (failed["message"] or ""), failed["message"]
    assert props(server, graph, robot(server)) == robot_before, "the failed batch is rolled back"
    # Ada's row, written before the Robot's, is in a batch committed before the failed one.
    assert props(server, graph, ada) != ada_before, "a committed batch is kept"
    refused(server, graph)
    server.unstop(graph)

    dry = migrate(server, graph, batch_size=2, dry_run=True)
    assert dry["status"] == "inProgress", dry["message"]
    other = migrate(server, graph, DECLARATIONS[:-1], batch_size=2)
    assert other["status"] == "refused", other["message"]
    assert props(server, graph, robot(server)) == robot_before, "neither writes"
    resume_to_the_end(server, graph, later)


def test_a_value_not_of_its_declared_kind_refuses_the_graph_before_anything_is_written(server: Server):
    graph = graph_name("invalid")
    write_layout1(server, graph)
    untouched = snapshot(server, graph)
    wrong = [*DECLARATIONS, Declaration("Person", "name", "date")]
    refused_invalid = migrate(server, graph, wrong)
    assert refused_invalid["status"] == "invalid", refused_invalid["message"]
    assert snapshot(server, graph) == untouched, "an invalid graph is refused before any write"
    kept = migrate(server, graph, wrong, dry_run=True, keep_invalid=True)
    assert kept["status"] == "dryRun", kept["message"]
    assert any(it["invalid"] for it in kept["properties"]), kept["properties"]
    assert snapshot(server, graph) == untouched


def resume_to_the_end(server: Server, graph: str, later: str) -> None:
    """Resumes a stopped run past the collision it then meets, at the row `later`, and checks the graph it leaves."""
    stopped = migrate(server, graph, batch_size=2)
    assert stopped["status"] == "collisions", stopped["message"]
    server.execute(f"DELETE FROM {graph}_node WHERE nid = {later}")
    migrated = migrate(server, graph, batch_size=2)
    assert migrated["status"] == "migrated", migrated["message"]
    reads_as_layout_2(server, graph)
    same_as_written(server, graph)


def reads_as_layout_2(server: Server, graph: str) -> None:
    """This version reads the graph as layout 2: its models loaded with their values' kinds, a `$` key as the user's."""
    registry = registry_on(server.store, graph)
    try:
        pm = PersistenceManagerFactory(registry).get(server.store)
        gom = StatelessGraphObjectManager(pm)

        def one(statement: str) -> Any:
            [found] = pm.query(QuerySpecification[Any](cypher_statement(statement)))
            return found

        assert gom.load(Person, "Ada") == Person(name="Ada", born=date(1815, 12, 10), height=1.65, visits=3)
        assert one("MATCH (p:Person) WHERE p.born < date('1800-01-01') RETURN p.name") == "Charles"
        assert one("MATCH (p:Person {name: 'Charles'}) RETURN p.height") == 2.0
        assert one("MATCH (p:Person {name: 'Charles'}) RETURN toString(p.height)") == "2.0"
        circle = gom.load(Circle, "Bob")
        assert circle is not None
        assert [(it.since, it.target.name) for it in circle.knows] == [(date(2020, 1, 1), "Cy")]
        assert one("MATCH ()-[k:KNOWS]->() RETURN k.since = date('2020-01-01')") is True
        assert gom.load(Meeting, "m1") == Meeting(id="m1", at=datetime(2024, 3, 15, 10, 30, 0, 500000, tzinfo=UTC))
        assert gom.load(File, "f1") == File(name="f1", data=b"\x00\x01\xfe\xff")
        assert one("MATCH (r:Robot) RETURN r.`$t`") == 1
        assert one("MATCH (r:Robot) RETURN r.`$x`") == "y"
        # An undeclared text that reads as a date stays a text.
        assert one("MATCH (p:Person {name: 'Mary'}) RETURN p.nick") == "2024-03-15"
    finally:
        registry.close()


NATIVE = [
    "CREATE (:Robot {`$t`: 1, `$x`: 'y', name: 'R2'})",
    "CREATE (:Event {at: datetime('2024-03-15T10:30:00.5Z')})",
]
"""
The statements of scripts/write_migration_fixture_0_8_0.py, but the second spelling of the Event's moment, and the first
written as the datetime its declaration makes of 0.8.0's text; its bytes saved as the bytes they are, not base64.
"""


def write_native(server: Server, graph: str) -> None:
    """Writes, with this version, the graph 0.8.0's writer writes, less the collision: see NATIVE."""
    registry = registry_on(server.store, graph)
    try:
        pm = PersistenceManagerFactory(registry).get(server.store)
        gom = StatelessGraphObjectManager(pm)
        gom.save(Person(name="Ada", born=date(1815, 12, 10), height=1.65, visits=3))
        gom.save(Person(name="Charles", born=date(1791, 12, 26), height=2.0))
        gom.save(Person(name="Mary", nick="2024-03-15"))
        gom.save(Circle(person=Person(name="Bob"), knows=[Knows(since=date(2020, 1, 1), target=Person(name="Cy"))]))
        gom.save(Meeting(id="m1", at=datetime(2024, 3, 15, 10, 30, 0, 500000, tzinfo=UTC)))
        # In Cypher, as 0.8.0's writer wrote it, but as the bytes they are.
        pm.execute(
            QuerySpecification[Any](cypher_statement("CREATE (:File {name: 'f1', data: $data})")).bind(
                {"data": b"\x00\x01\xfe\xff"}
            )
        )
        for statement in NATIVE:
            pm.execute(QuerySpecification[Any](cypher_statement(statement)))
    finally:
        registry.close()


def schema_made(server: Server, graph: str) -> str:
    """Makes the application's schema in the graph, as 0.8.0's writer made it, and gives the store's identity."""
    registry = registry_on(server.store, graph)
    try:
        gom = StatelessGraphObjectManager(PersistenceManagerFactory(registry).get(server.store))
        gom.constraints.ensure(uniqueness_constraint("Person", "name"))
        gom.constraints.ensure(uniqueness_constraint("Event", "at"))
        # The store's identity, which a graph this version writes is given when first asked: the migrated graph's
        # stamp, kept from layout 1, is given one as that graph's would be.
        return (gom.store_identity()).id
    finally:
        registry.close()


def same_as_written(server: Server, graph: str) -> None:
    """
    The graph, once its schema is made, against the graph this version writes from the fixture's models and
    statements, less the collision: the same nodes and relationships, property for property, each value of the same
    type, the same stamp and marker, and the same indexes.
    """
    native = graph_name("native")
    write_native(server, native)
    identities = {schema_made(server, native), schema_made(server, graph)}
    assert len(identities) == 2, f"each graph has an identity of its own: {identities}"
    rows, migrated_rows = contents(server, native), contents(server, graph)
    assert rows == migrated_rows, (
        "the migrated graph's rows are the ones written: written alone "
        f"{sorted(set(rows) - set(migrated_rows))}, migrated alone {sorted(set(migrated_rows) - set(rows))}"
    )
    tables = others(server, graph)
    assert "<g>_schema" in tables and any(it.startswith("  ") for it in tables), f"the marker is compared: {tables}"
    assert others(server, native) == tables, "and its other tables: the marker, the lock"
    assert len(server.indexes(graph)) >= 2, f"the indexes are compared: {server.indexes(graph)}"
    written = {it.replace(native, "<g>").replace(native.upper(), "<G>") for it in server.indexes(native)}
    migrated = {it.replace(graph, "<g>").replace(graph.upper(), "<G>") for it in server.indexes(graph)}
    assert written == migrated, f"and its indexes: {written - migrated} against {migrated - written}"


COUNTS = ("<g>_count2", "<g>_count2_delta")
"""The count store's tables, which the migration leaves unseeded (drivine-cypher-spec#349): their rows not compared."""


def others(server: Server, graph: str) -> list[str]:
    """Each table of the graph but its nodes and relationships, and its rows, the graph's name written `<g>`."""
    found: list[str] = []
    for table in server.tables(graph):
        named = table.replace(graph, "<g>")
        if named in ("<g>_node", "<g>_edge"):
            continue
        found.append(named)
        if named not in COUNTS:
            found += sorted(
                "  " + json.dumps(row, sort_keys=True, default=str).replace(graph, "<g>").replace(graph.upper(), "<G>")
                for row in server.table_rows(table)
            )
    return found


def contents(server: Server, graph: str) -> list[str]:
    """
    The nodes and relationships of the graph, each with its columns but its id, its JSON as JSON that tells each value's
    type: a relationship's ends named by their node, as the ids differ. A stamp is told by its form alone, as each save
    draws new tokens (the module's `Stamps`); the store's own node gives its id and when it was assigned, which are the
    graph's own.
    """
    nodes: dict[Any, str] = {}
    for row in server.table_rows(f"{graph}_node"):
        nid = row.pop("nid")
        row["stamp"] = stamp_form(row.get("stamp"))
        if "DrivineStore" in (row.get("labels") or []) and isinstance(row.get("props"), dict):
            for it in ("storeId", "assignedAt"):
                row["props"].pop(it, None)
        nodes[nid] = json.dumps(row, sort_keys=True, default=str)
    edges = []
    for row in server.table_rows(f"{graph}_edge"):
        row.pop("eid", None)
        src, dst = nodes[row.pop("src")], nodes[row.pop("dst")]
        edges.append(f"({src})-[{json.dumps(row, sort_keys=True, default=str)}]->({dst})")
    return sorted([*nodes.values(), *edges])


STAMP = re.compile(r"[0-9a-f]{16}:([0-9a-f]{16})?")
"""A stamp's form: a node token and a links token, the links token empty on a node no relationship has touched."""


def stamp_form(stamp: Any) -> Any:
    """A stamp of the form a save writes, as that form and whether it has a links token; anything else as it is."""
    if isinstance(stamp, str) and STAMP.fullmatch(stamp):
        return "<node>:<links>" if not stamp.endswith(":") else "<node>:"
    return stamp
