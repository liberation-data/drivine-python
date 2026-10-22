"""
Writes a graph into a SQL store's tables directly, so reads are tested before writes are, as TypeScript's SqlSeed: on
Postgres and on Oracle, each in its own SQL.
"""

import json
from typing import Any, Literal

from drivine import DatabaseType, PersistenceManager, QuerySpecification
from drivine.backend.postgres.postgres_graph import PostgresGraph


def raw_property(pm: PersistenceManager, property: str, as_: Literal["TEXT", "NUMBER"] = "TEXT") -> str:
    """A raw `where` over the node row `n`, in the store's SQL: a property as text, or as a number."""
    if pm.type is DatabaseType.ORACLE:
        return f"JSON_VALUE(n.props, '$.{property}'{' RETURNING NUMBER' if as_ == 'NUMBER' else ''})"
    return f"CAST(n.props ->> '{property}' AS numeric)" if as_ == "NUMBER" else f"(n.props ->> '{property}')"


def raw_props(pm: PersistenceManager, scope: str) -> str:
    """
    The statement that reads the properties of the one node carrying `scope`, as the store holds them: a row of one
    column, `props`. Oracle's are re-encoded, as the thin driver can't read a document updated in place
    (oracle/python-oracledb#622).
    """
    if pm.type is DatabaseType.ORACLE:
        return (
            "SELECT JSON_TRANSFORM(n.props, KEEP '$') AS \"props\" FROM drivine_node n "
            f"WHERE JSON_EXISTS(n.labels, '$?(@ == \"{scope}\")')"
        )
    return f'SELECT n.props AS "props" FROM drivine_node n WHERE n.labels @> \'["{scope}"]\''


class SqlSeed:
    """
    Nodes by labels and properties, and relationships between nodes found by a key property among those carrying
    `scope`.
    """

    def __init__(self, pm: PersistenceManager, scope: str, key: str = "id") -> None:
        self.pm = pm
        self.scope = scope
        self.key = key
        self.oracle = pm.type is DatabaseType.ORACLE

    async def prepare(self) -> None:
        """Makes the store where it is not there: Oracle's connection makes it; on Postgres, an object manager does."""
        if not self.oracle:
            await self.pm.execute(QuerySpecification[Any](PostgresGraph().ensuring))

    async def clear(self) -> None:
        await self.pm.execute(QuerySpecification[Any](f"DELETE FROM drivine_node n WHERE {self._carries('n')}"))

    async def node(self, labels: list[str], props: dict[str, Any], stamp: str | None = None) -> None:
        text = (
            "INSERT INTO drivine_node (labels, props, stamp) VALUES (JSON($labels), JSON($props), $stamp)"
            if self.oracle
            else "INSERT INTO drivine_node (labels, props, stamp) VALUES (CAST($labels AS jsonb), "
            "CAST($props AS jsonb), CAST($stamp AS varchar))"
        )
        await self.pm.execute(
            QuerySpecification[Any](text).bind(
                {"labels": json.dumps([self.scope, *labels]), "props": json.dumps(props), "stamp": stamp}
            )
        )

    async def edge(self, from_: str, type: str, to: str, props: dict[str, Any] | None = None) -> None:
        def at(alias: str) -> str:
            if self.oracle:
                return f"{self._carries(alias)} AND JSON_VALUE({alias}.props, '$.{self.key}') = ${alias}"
            return f"{self._carries(alias)} AND {alias}.props ->> '{self.key}' = CAST(${alias} AS text)"

        values = "$type, JSON($props)" if self.oracle else "CAST($type AS text), CAST($props AS jsonb)"
        await self.pm.execute(
            QuerySpecification[Any](
                f"INSERT INTO drivine_edge (src, dst, type, props) SELECT a.nid, b.nid, {values} "
                f"FROM drivine_node a, drivine_node b WHERE {at('a')} AND {at('b')}"
            ).bind({"a": from_, "b": to, "type": type, "props": json.dumps(props or {})})
        )

    def _carries(self, alias: str) -> str:
        """Whether the node row carries the seed's scope label."""
        if self.oracle:
            return f"JSON_EXISTS({alias}.labels, '$?(@ == \"{self.scope}\")')"
        return f"{alias}.labels @> '[\"{self.scope}\"]'"
