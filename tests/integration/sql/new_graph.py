"""A graph no provider has made yet, on a SQL store, named as the environment names one, and dropped after."""

from __future__ import annotations

import os
import uuid
from typing import Any

from drivine import DatabaseRegistry, DatabaseType, PersistenceManager, QuerySpecification


def new_graph_registry(store: str) -> tuple[DatabaseRegistry, str]:
    """
    A registry of its own, so a provider of its own, for the SQL store registered as `store`, over a graph of a new
    name given as `<STORE>_DATABASE_DEFAULT_GRAPH_PATH`.
    """
    name = f"first_use_{uuid.uuid4().hex[:8]}"
    registry = DatabaseRegistry()
    registry.build_or_resolve_from_env(store, {**os.environ, f"{store.upper()}_DATABASE_DEFAULT_GRAPH_PATH": name})
    return registry, name


async def tables_made(pm: PersistenceManager, name: str) -> int:
    """How many of the graph's node, edge and catalog tables there are."""
    tables = ", ".join(f"'{name}_{it}'" for it in ("node", "edge", "schema"))
    if pm.type is DatabaseType.ORACLE:
        return await pm.get_one(
            QuerySpecification[Any](f"SELECT COUNT(*) FROM user_tables WHERE LOWER(table_name) IN ({tables})")
        )
    row = await pm.get_one(
        QuerySpecification[Any](f"SELECT COUNT(*) AS made FROM pg_tables WHERE tablename IN ({tables})")
    )
    return row["made"]


async def drop_graph(pm: PersistenceManager, name: str) -> None:
    """Drops the graph's tables, and on Oracle the property graph over them."""
    if pm.type is DatabaseType.ORACLE:
        statements = [f"DROP PROPERTY GRAPH IF EXISTS {name}"] + [
            f"DROP TABLE IF EXISTS {name}_{it} PURGE" for it in ("edge", "node", "schema")
        ]
    else:
        # CASCADE: the store's functions return a row of its node table.
        statements = [f"DROP TABLE IF EXISTS {name}_edge, {name}_node, {name}_schema CASCADE"]
    for statement in statements:
        await pm.execute(QuerySpecification[Any](statement))
