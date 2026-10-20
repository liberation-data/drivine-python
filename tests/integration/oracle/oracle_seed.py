"""Writes a graph into Oracle's tables directly, so reads are tested before writes are, as TypeScript's OracleSeed."""

import json
import os
from typing import Any

import pytest

from drivine import PersistenceManager, QuerySpecification

ON_ORACLE = pytest.mark.skipif(
    os.environ.get("ORACLE_DATABASE_TYPE") != "ORACLE", reason="Oracle runs only with ORACLE_TESTS=true."
)
"""Skips a suite unless Oracle is configured."""


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
