# Written by scripts/unasync.py from drivine/schema/schema_manager.py:
# change that, and run the script again.
from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Mapping
from typing import Any

from drivine.backend.sql.sql_catalog import CATALOG_KINDS
from drivine.connection.database_type import DatabaseType
from drivine.drivine_error import DrivineError
from drivine.schema.ensure_result import EnsureResult
from drivine.schema.schema_catalog import SchemaCatalog
from drivine.schema.schema_specs import SchemaSpec, item_name, uniqueness_constraint
from drivine.schema.uniqueness_constraint_spec import UniquenessConstraintSpec
from drivine.schema.vector_index_spec import VectorIndexSpec
from drivine.sync.connection.database_registry import DatabaseRegistry
from drivine.sync.manager.graph_object_manager_factory import GraphObjectManagerFactory
from drivine.sync.manager.stateless_graph_object_manager import StatelessGraphObjectManager
from drivine.sync.schema.schema_options import SchemaOptions

logger = logging.getLogger("drivine.schema")

GRAPH_TYPES = (
    DatabaseType.NEO4J,
    DatabaseType.MEMGRAPH,
    DatabaseType.FALKORDB,
    DatabaseType.ORACLE,
    DatabaseType.POSTGRES,
)

MARKER_LABEL = "_DrivineSchema"
"""The label of the schema markers: drivine4j's and TypeScript's, so all read one."""


def _key_of(spec: SchemaSpec) -> str:
    return f"{spec.kind}:{spec.label}:{','.join(sorted(spec.properties))}"


def inventory_key(spec: SchemaSpec) -> str:
    """
    An item's identity in an owner's inventory, drivine4j's `inventoryKey`: kind, label, properties and name, so a
    renamed item shows as an orphan rather than coexisting unattributed.
    """
    return f"{CATALOG_KINDS[spec.kind]}:{spec.label}:{','.join(spec.properties)}:{item_name(spec)}"


class SchemaManager:
    """
    Holds catalogs of indexes and constraints on the databases they apply to. `enforce` is idempotent: it makes what is
    missing, and recreates a catalog's items when its version changed. `recreate_all` drops and makes everything.
    """

    def __init__(self, registry: DatabaseRegistry, factory: GraphObjectManagerFactory, options: SchemaOptions) -> None:
        self.registry = registry
        self.factory = factory
        self.options = options

    def enforce(self) -> dict[str, list[EnsureResult[Any]]]:
        """Ensures every catalog on its databases: indexes before constraints. Returns the results, by database."""
        return self._apply(False)

    def recreate_all(self) -> dict[str, list[EnsureResult[Any]]]:
        """Drops and makes every declared item. Destructive."""
        return self._apply(True)

    def _apply(self, recreate: bool) -> dict[str, list[EnsureResult[Any]]]:
        report: dict[str, list[EnsureResult[Any]]] = {}
        for database, catalogs in self._catalogs_by_database().items():
            gom = self.factory.get(database)
            items = self._merged(database, catalogs)
            owners: dict[str, list[SchemaCatalog]] = {}
            for it in catalogs:
                owners.setdefault(it.name, []).append(it)
            co_owned = _co_owned_keys(owners)
            results: list[EnsureResult[Any]] = []
            for owner, own in owners.items():
                results.extend(self._apply_owner(gom, database, owner, own, items, co_owned, recreate))
            report[database] = results
        return report

    def _apply_owner(
        self,
        gom: StatelessGraphObjectManager,
        database: str,
        owner: str,
        catalogs: list[SchemaCatalog],
        items: Mapping[str, SchemaSpec],
        co_owned: set[str],
        forced: bool,
    ) -> list[EnsureResult[Any]]:
        """
        Applies one owner's items: recreated when forced, when `recreate_on_startup`, or when the owner's version
        changed since it last applied (a first version is adopted, not acted on); an item another owner declares too is
        never recreated by one owner's version. Items the owner applied before and no longer declares (orphans) are
        reported, never dropped. The owner's version and inventory are recorded only when the apply succeeds, so a
        failed one is tried again on the next enforce.
        """
        versions = sorted({it.version for it in catalogs if it.version is not None})
        version = "|".join(versions) if versions else None
        pm = gom.persistence_manager
        if version is not None:
            self._ensure_singleton(gom, owner)
        stored = (
            pm.maybe_get_one(gom.backend.schema_marker("version").spec({"scope": owner}))
            if version is not None
            else None
        )
        version_changed = version is not None and stored is not None and stored != version
        own = [items[key] for key in dict.fromkeys(_key_of(spec) for it in catalogs for spec in it.items)]
        declared = [inventory_key(it) for it in own]
        self._report_orphans(gom, database, owner, declared, co_owned)
        ordered = [*(it for it in own if it.kind != "UNIQUE"), *(it for it in own if it.kind == "UNIQUE")]
        results: list[EnsureResult[Any]] = []
        for spec in ordered:
            recreate = forced or (
                (self.options.recreate_on_startup or version_changed) and inventory_key(spec) not in co_owned
            )
            results.append(self._ensure(gom, spec, recreate))
        pm.execute(
            gom.backend.record_schema_marker().spec({"scope": owner, "version": version, "items": "\n".join(declared)})
        )
        return results

    def _ensure_singleton(self, gom: StatelessGraphObjectManager, owner: str) -> None:
        """
        Keeps one marker for each owner: drops all but the newest a race left, and backs the marker's key with a
        uniqueness constraint, so two processes enforcing at once can't each make one.
        """
        gom.persistence_manager.execute(gom.backend.prune_schema_markers().spec({"scope": owner}))
        gom.constraints.ensure(uniqueness_constraint(MARKER_LABEL, "scope"))

    def _report_orphans(
        self, gom: StatelessGraphObjectManager, database: str, owner: str, declared: list[str], co_owned: set[str]
    ) -> None:
        stored = gom.backend.marker_items(
            gom.persistence_manager.maybe_get_one(gom.backend.schema_marker("items").spec({"scope": owner}))
        )
        if stored is None:
            return
        orphans = [it for it in stored if it not in declared and it not in co_owned]
        if orphans:
            logger.warning(
                "Owner '%s' on %s applied %s item(s) it no longer declares: %s. They are left in place; drop them, or "
                "recreate, if unwanted.",
                owner,
                database,
                len(orphans),
                ", ".join(orphans),
            )

    def _ensure(self, gom: StatelessGraphObjectManager, spec: SchemaSpec, recreate: bool) -> EnsureResult[Any]:
        if isinstance(spec, UniquenessConstraintSpec):
            if recreate:
                gom.constraints.drop(spec)
            return self._judge(gom.constraints.ensure(spec, self.options.violation_sample_size))
        if recreate:
            return gom.indexes.recreate(spec)
        result = gom.indexes.ensure(spec)
        if result.status == "DRIFT" and self.options.recreate_on_drift:
            return gom.indexes.recreate(spec)
        return self._judge(result)

    def _judge(self, result: EnsureResult[Any]) -> EnsureResult[Any]:
        if result.status in ("DRIFT", "VIOLATION"):
            spec = result.spec
            message = f"{result.status} for the {spec.kind} on {spec.label}({', '.join(spec.properties)})"
            if result.sample:
                message += (
                    f": keys held more than once, with nodes that hold them, include "
                    f"{json.dumps(list(result.sample), default=str)}"
                )
            if result.found:
                message += f": the engine holds {result.found}"
            if self.options.mode == "WARN":
                logger.warning(message)
            else:
                raise DrivineError(message)
        return result

    def _catalogs_by_database(self) -> dict[str, list[SchemaCatalog]]:
        """The catalogs that apply to each graph database. A named database that is unknown or not a graph fails."""
        graphs = [it.name for it in self.registry.providers if it.type in GRAPH_TYPES]
        result: dict[str, list[SchemaCatalog]] = {}
        for catalog in self.options.catalogs:
            targets: list[str] = graphs
            if catalog.databases is not None:
                targets = []
                for name in catalog.databases:
                    provider = self.registry.connection_provider(name)
                    if provider is None or provider.type not in GRAPH_TYPES:
                        raise DrivineError(f"A schema catalog names {name}, which is not a registered graph database.")
                    targets.append(name)
            for name in targets:
                result.setdefault(name, []).append(catalog)
        return result

    def _merged(self, database: str, catalogs: list[SchemaCatalog]) -> dict[str, SchemaSpec]:
        """
        The items of a database's catalogs, each once. Two declarations of one item must agree on everything portable;
        where one of them carries engine options it wins whole, and two that carry different engine options conflict.
        """
        items: dict[str, SchemaSpec] = {}
        for catalog in catalogs:
            for spec in catalog.items:
                held = items.get(_key_of(spec))
                items[_key_of(spec)] = self._reconciled(database, held, spec) if held is not None else spec
        return items

    @staticmethod
    def _reconciled(database: str, held: SchemaSpec, spec: SchemaSpec) -> SchemaSpec:
        def options_of(it: SchemaSpec) -> tuple[Any, ...]:
            return it.engine_options if isinstance(it, VectorIndexSpec) else ()

        def portable(it: SchemaSpec) -> SchemaSpec:
            if isinstance(it, UniquenessConstraintSpec):
                return dataclasses.replace(it, node_id_property=None)
            return dataclasses.replace(it, engine_options=()) if isinstance(it, VectorIndexSpec) else it

        if isinstance(held, UniquenessConstraintSpec) and isinstance(spec, UniquenessConstraintSpec):
            # The node id names a violation's nodes and makes nothing: two that differ in it alone are one, keeping it.
            if portable(held) == portable(spec):
                return held if held.node_id_property is not None else spec
        a, b = options_of(held), options_of(spec)
        if portable(held) == portable(spec):
            if not b or a == b:
                return held
            if not a:
                return spec
        raise DrivineError(
            f"Two schema catalogs declare the {spec.kind} on {spec.label}({', '.join(spec.properties)}) on {database} "
            f"differently."
        )


def _co_owned_keys(owners: Mapping[str, list[SchemaCatalog]]) -> set[str]:
    """The inventory keys more than one owner declares."""
    counts: dict[str, int] = {}
    for catalogs in owners.values():
        for key in {inventory_key(spec) for it in catalogs for spec in it.items}:
            counts[key] = counts.get(key, 0) + 1
    return {key for key, count in counts.items() if count > 1}
