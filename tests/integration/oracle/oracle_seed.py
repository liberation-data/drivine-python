"""Writes a graph into Oracle's tables directly, so reads are tested before writes are, as TypeScript's OracleSeed."""

import json
import os
from typing import Any

import oracledb
import pytest

from drivine import ConnectionProperties, PersistenceManager, QuerySpecification
from drivine.backend.sql.sql_graph import sql_text
from drivine.testing.drivine_test_containers import TEST_CONTAINER_PASSWORD

ON_ORACLE = pytest.mark.skipif(
    os.environ.get("ORACLE_DATABASE_TYPE") != "ORACLE", reason="Oracle runs only with ORACLE_TESTS=true."
)
"""Skips a suite unless Oracle is configured."""


async def oracle_plan(pm: PersistenceManager, sql: str, parameters: dict[str, Any], format: str) -> str:
    """
    The plan Oracle makes for `sql` on `pm`, its parameters written in as literals so that the plan is the one for
    them, shown in the format `DBMS_XPLAN.DISPLAY` takes. Explained and read in one block, so on one session.
    """
    for name, value in sorted(parameters.items(), key=lambda it: -len(it[0])):
        sql = sql.replace(f"${name}", "NULL" if value is None else sql_text(str(value)))
    block = (
        "DECLARE\n    v_plan SYS_REFCURSOR;\nBEGIN\n"
        f"    EXECUTE IMMEDIATE {sql_text(f'EXPLAIN PLAN FOR {sql}')};\n"
        f"    OPEN v_plan FOR SELECT plan_table_output FROM TABLE(DBMS_XPLAN.DISPLAY(format => '{format}'));\n"
        "    DBMS_SQL.RETURN_RESULT(v_plan);\nEND;"
    )
    rows = await pm.query(QuerySpecification[Any](block))
    return "\n".join(str(it if not isinstance(it, dict) else next(iter(it.values()))) for it in rows)


async def oracle_last_plan(sid: str, format: str) -> str:
    """
    The plan of the statement the session `sid` ran last, as the child cursor that ran it holds it, shown in the
    format `DBMS_XPLAN.DISPLAY_CURSOR` takes: a plan Oracle made again on a later run, from what an earlier run found
    (statistics feedback), is a child of its own. Read as the container's system user, as the test user reads no `V$`
    view. drivine4j's `OracleTestDatabase.lastPlan`.
    """
    it = ConnectionProperties.from_env("oracle")
    admin = await oracledb.connect_async(
        user="system", password=TEST_CONTAINER_PASSWORD, dsn=f"{it.host}:{it.port or 1521}/{it.database_name}"
    )
    try:
        cursor = admin.cursor()
        try:
            await cursor.execute("SELECT prev_sql_id, prev_child_number FROM v$session WHERE sid = :sid", sid=int(sid))
            session = await cursor.fetchone()
            assert session is not None, f"No session {sid}."
            await cursor.execute(
                "SELECT plan_table_output FROM TABLE(DBMS_XPLAN.DISPLAY_CURSOR(:sql_id, :child, :format))",
                sql_id=session[0],
                child=session[1],
                format=format,
            )
            return "\n".join(str(row[0]) for row in await cursor.fetchall())
        finally:
            cursor.close()
    finally:
        await admin.close()


class OracleSeed:
    """
    Nodes by labels and properties, and relationships between nodes found by a key property among those carrying
    `scope`.
    """

    def __init__(self, pm: PersistenceManager, scope: str, key: str = "id") -> None:
        self.pm = pm
        self.scope = scope
        self.key = key

    async def clear(self) -> None:
        await self.pm.execute(
            QuerySpecification[Any](
                f"DELETE FROM drivine_node n WHERE JSON_EXISTS(n.labels, '$?(@ == \"{self.scope}\")')"
            )
        )

    async def node(self, labels: list[str], props: dict[str, Any], stamp: str | None = None) -> None:
        await self.pm.execute(
            QuerySpecification[Any](
                "INSERT INTO drivine_node (labels, props, stamp) VALUES (JSON($labels), JSON($props), $stamp)"
            ).bind({"labels": json.dumps([self.scope, *labels]), "props": json.dumps(props), "stamp": stamp})
        )

    async def edge(self, from_: str, type: str, to: str, props: dict[str, Any] | None = None) -> None:
        def at(alias: str) -> str:
            return (
                f"JSON_EXISTS({alias}.labels, '$?(@ == \"{self.scope}\")') "
                f"AND JSON_VALUE({alias}.props, '$.{self.key}') = ${alias}"
            )

        await self.pm.execute(
            QuerySpecification[Any](
                "INSERT INTO drivine_edge (src, dst, type, props) SELECT a.nid, b.nid, $type, JSON($props) "
                f"FROM drivine_node a, drivine_node b WHERE {at('a')} AND {at('b')}"
            ).bind({"a": from_, "b": to, "type": type, "props": json.dumps(props or {})})
        )
