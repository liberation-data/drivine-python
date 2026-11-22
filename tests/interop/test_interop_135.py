"""
drivine-python's half of a run of drivine-cypher-spec#135's declarations across the three libraries over one store: for
each case of declarations.yaml, on a graph of its own, declares it in this library's API, as
tests/unit/test_declarations_spec.py does, and enforces it, writing what came of it to a report a driver judges against
the other libraries' halves. Drivine for TypeScript's Interop135.spec, ported. Run only where the driver sets
INTEROP_STORE, with `-o drivine_test_containers=false`, as the driver starts the stores; it asserts nothing itself.

INTEROP_STORE (sqlite, postgres or oracle), INTEROP_GRAPH (each case's graph is `<prefix>_<i>`, `i` counted from 0 in
the cases' order), INTEROP_PHASE (write or check), INTEROP_CASES (declarations.yaml), INTEROP_REPORT (where the report
goes), and the store's INTEROP_HOST, INTEROP_PORT, INTEROP_USER, INTEROP_PASSWORD and INTEROP_DATABASE (Postgres's
database, Oracle's service, or the directory of SQLite's `<graph>.db` files).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

import drivine.schema.schema_plan_driver as schema_plan_driver
from drivine import (
    ConnectionProperties,
    DatabaseRegistry,
    DatabaseType,
    PersistenceManager,
    PersistenceManagerFactory,
    QuerySpecification,
    SchemaCatalog,
    SchemaManager,
    SchemaOptions,
    cypher_statement,
)
from drivine.manager.graph_object_manager_factory import GraphObjectManagerFactory
from tests.unit.test_declarations_spec import CASES, DECLARATIONS, GAPS

ENV = os.environ

pytestmark = [
    pytest.mark.skipif(not ENV.get("INTEROP_STORE"), reason="Run by drivine-cypher-spec#135's driver, which sets it."),
    pytest.mark.async_only,
]

TYPES = {"sqlite": DatabaseType.SQLITE, "postgres": DatabaseType.POSTGRES, "oracle": DatabaseType.ORACLE}

RUN_CASES: list[dict[str, Any]] = (
    yaml.safe_load(Path(ENV["INTEROP_CASES"]).read_text())["cases"] if ENV.get("INTEROP_CASES") else CASES
)
"""The driver's cases, or, in a run that isn't one of the driver's, which skips them, this copy's."""

REPORTS: list[dict[str, Any]] = []


@pytest.fixture(scope="module", autouse=True)
def report() -> Iterator[None]:
    """Writes the report of every case once they have run."""
    yield
    if ENV.get("INTEROP_REPORT"):
        Path(ENV["INTEROP_REPORT"]).write_text(
            json.dumps({"phase": ENV.get("INTEROP_PHASE"), "reports": REPORTS}, indent=2, ensure_ascii=False)
        )


def registered(graph: str) -> DatabaseRegistry:
    """A registry of the store's graph `graph`, under that name, as the driver set the store out."""
    store = TYPES[ENV["INTEROP_STORE"]]
    sqlite = store is DatabaseType.SQLITE
    registry = DatabaseRegistry()
    registry.builder().with_properties(
        ConnectionProperties(
            database_type=store,
            host="" if sqlite else ENV["INTEROP_HOST"],
            port=None if sqlite else int(ENV["INTEROP_PORT"]),
            user_name=ENV.get("INTEROP_USER"),
            password=ENV.get("INTEROP_PASSWORD"),
            database_name=str(Path(ENV["INTEROP_DATABASE"]) / f"{graph}.db") if sqlite else ENV.get("INTEROP_DATABASE"),
            default_graph_path=graph,
        )
    ).register(graph)
    return registry


async def marker(pm: PersistenceManager | None) -> str | None:
    """The store's schema markers, as Cypher reads them, written as JavaScript's JSON.stringify writes them."""
    if pm is None:
        return None
    try:
        rows = await pm.query(
            QuerySpecification[Any](cypher_statement("MATCH (m:_DrivineSchema) RETURN properties(m) AS marker"))
        )
    except Exception as error:
        return f"unread: {error}"
    return json.dumps(rows, separators=(",", ":"), ensure_ascii=False)


@pytest.mark.parametrize(("index", "name"), [(i, it["case"]) for i, it in enumerate(RUN_CASES)])
async def test_runs_the_case(index: int, name: str, monkeypatch: pytest.MonkeyPatch):
    graph = f"{ENV['INTEROP_GRAPH']}_{index}"
    reported: dict[str, Any] = {
        "case": name,
        "outcome": None,
        "statements": [],
        "markerBefore": None,
        "markerAfter": None,
        "failure": None,
        **({"gap": GAPS[name]} if name in GAPS else {}),
    }
    REPORTS.append(reported)
    ran: list[dict[str, Any]] = []
    answer = schema_plan_driver._answer  # pyright: ignore[reportPrivateUsage]

    async def recorded(action: Any, host: Any) -> Any:
        # Each statement the plan runs for what it does, with its purpose as the module names it, run as it would be.
        if action["type"] == "execute":
            statement = action["statement"]
            ran.append(
                {
                    "purpose": action["purpose"],
                    "statement": statement if isinstance(statement, str) else json.dumps(statement),
                }
            )
        return await answer(action, host)

    monkeypatch.setattr(schema_plan_driver, "_answer", recorded)
    registry: DatabaseRegistry | None = None
    pm: PersistenceManager | None = None
    try:
        # Inside the try, so that a store that can't be registered is the case's failure, in the report.
        registry = registered(graph)
        pm = PersistenceManagerFactory(registry).get(graph)
        reported["markerBefore"] = await marker(pm)
        declare = DECLARATIONS.get(name)
        if declare is None:
            raise LookupError(f"No declaration for the spec's case '{name}'.")
        declared = declare()
        manager = SchemaManager(
            registry,
            GraphObjectManagerFactory(PersistenceManagerFactory(registry)),
            SchemaOptions([it.for_database(graph) for it in declared.catalogs]),
        )
        for owner, specs in declared.runtime:
            await manager.ensure_catalog(SchemaCatalog(specs, name=owner).for_database(graph))
        results = await manager.enforce()
        reported["outcome"] = [it.status for found in results.values() for it in found]
    except Exception as error:
        reported["failure"] = str(error) or type(error).__name__
    finally:
        reported["statements"] = ran
        monkeypatch.undo()
        reported["markerAfter"] = await marker(pm)
        if registry is not None:
            await registry.close()
