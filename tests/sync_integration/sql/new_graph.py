# Written by scripts/unasync.py from tests/integration/sql/new_graph.py:
# change that, and run the script again.
"""A graph no provider has made yet, on a SQL store, named as the environment names one, and dropped after."""

from __future__ import annotations

import os
import uuid
from typing import Any

from drivine.sync import DatabaseRegistry, DatabaseType, PersistenceManager, QuerySpecification


def new_graph_registry(store: str) -> tuple[DatabaseRegistry, str]:
    """
    A registry of its own, so a provider of its own, for the SQL store registered as `store`, over a graph of a new
    name given as `<STORE>_DATABASE_DEFAULT_GRAPH_PATH`.
    """
    name = f"first_use_{uuid.uuid4().hex[:8]}"
    return registry_on(store, name), name


def registry_on(store: str, name: str, pool_max: int | None = None) -> DatabaseRegistry:
    """A registry of its own, so a pool of its own, for the SQL store registered as `store`, over the graph `name`."""
    env = {**os.environ, f"{store.upper()}_DATABASE_DEFAULT_GRAPH_PATH": name}
    if pool_max is not None:
        env[f"{store.upper()}_DATABASE_POOL_MAX"] = str(pool_max)
    registry = DatabaseRegistry()
    registry.build_or_resolve_from_env(store, env)
    return registry


def tables_made(pm: PersistenceManager, name: str) -> int:
    """How many of the graph's node, edge and catalog tables there are."""
    tables = ", ".join(f"'{name}_{it}'" for it in ("node", "edge", "schema"))
    if pm.type is DatabaseType.ORACLE:
        return pm.get_one(
            QuerySpecification[Any](f"SELECT COUNT(*) FROM user_tables WHERE LOWER(table_name) IN ({tables})")
        )
    row = pm.get_one(QuerySpecification[Any](f"SELECT COUNT(*) AS made FROM pg_tables WHERE tablename IN ({tables})"))
    return row["made"]


def store_objects_made(pm: PersistenceManager, name: str) -> int:
    """
    How many of the graph's store objects there are: its node, edge and catalog tables, and on Oracle the property graph
    over them, made last. A whole store has all four, or three on Postgres.
    """
    made = tables_made(pm, name)
    if pm.type is DatabaseType.ORACLE:
        made += pm.get_one(
            QuerySpecification[Any](f"SELECT COUNT(*) FROM user_property_graphs WHERE graph_name = '{name.upper()}'")
        )
    return made


def drop_graph(pm: PersistenceManager, name: str) -> None:
    """Drops the graph's tables, and on Oracle the property graph over them."""
    if pm.type is DatabaseType.ORACLE:
        statements = [f"DROP PROPERTY GRAPH IF EXISTS {name}"] + [
            f"DROP TABLE IF EXISTS {name}_{it} PURGE" for it in ("edge", "node", "schema")
        ]
    else:
        # CASCADE: the store's functions return a row of its node table.
        statements = [f"DROP TABLE IF EXISTS {name}_edge, {name}_node, {name}_schema CASCADE"]
    for statement in statements:
        pm.execute(QuerySpecification[Any](statement))
