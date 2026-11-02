from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from drivine import concurrency
from drivine.backend.sql.sql_catalog import CATALOG_KINDS
from drivine.connection.database_registry import DatabaseRegistry
from drivine.connection.database_type import DatabaseType
from drivine.drivine_error import DrivineError
from drivine.manager.graph_object_manager_factory import GraphObjectManagerFactory
from drivine.manager.stateless_graph_object_manager import StatelessGraphObjectManager
from drivine.schema import vector_annotation_check
from drivine.schema.ensure_result import EnsureResult
from drivine.schema.recorded_inventory import RecordedInventory, inventory_entries, read_inventory
from drivine.schema.schema_catalog import DEFAULT_SCOPE, SchemaCatalog
from drivine.schema.schema_options import SchemaOptions
from drivine.schema.schema_specs import SchemaSpec, item_name, made_as, uniqueness_constraint
from drivine.schema.uniqueness_constraint_spec import UniquenessConstraintSpec
from drivine.schema.vector_index_spec import VectorIndexSpec

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
    """What makes two declarations the same item: kind, label and the properties it is made over (see `made_as`)."""
    return f"{spec.kind}:{spec.label}:{','.join(sorted(made_as(spec).properties))}"


def inventory_key(declared: SchemaSpec) -> str:
    """
    An item's identity in an owner's inventory, drivine4j's `inventoryKey`: kind, label, properties and name, so a
    renamed item shows as an orphan rather than coexisting unattributed. Of the item as it is made (see `made_as`): one
    that ignores case names the lowercased copies and its own name,
    `RANGE_INDEX:Account:__drivine.lower.email:Account_email_ci_range`.
    """
    spec = made_as(declared)
    return f"{CATALOG_KINDS[spec.kind]}:{spec.label}:{','.join(spec.properties)}:{item_name(spec)}"


@dataclass(frozen=True)
class _Plan:
    """What one database is to be given: each owner's catalogs, their items merged, and which are co-owned."""

    items: Mapping[str, SchemaSpec]
    owners: Mapping[str, list[SchemaCatalog]]
    co_owned: set[str]
    runtime_only: Mapping[str, set[str]]
    """By owner, the inventory keys of the items only `ensure` gave it, not one of its catalogs."""


class SchemaManager:
    """
    Holds catalogs of indexes and constraints on the databases they apply to. `enforce` is idempotent: it makes what is
    missing, and recreates a catalog's items when its version changed. `recreate_all` drops and makes everything.
    The catalogs are read once, from the options the manager is built with. Schema known only later is given to
    `ensure`, under an owner, and joins what that owner declared. With nothing to apply, no catalog or only empty ones
    and nothing ensured, it says so at INFO, naming the empty ones.
    Before anything is applied or kept, by `enforce`, `recreate_all` or `ensure`, a vector index over a fragment's
    field that has no `VectorIndex` is refused, naming the class, the field and the spec; a fragment whose model is
    built later is checked against the specs applied here.
    """

    def __init__(self, registry: DatabaseRegistry, factory: GraphObjectManagerFactory, options: SchemaOptions) -> None:
        self.registry = registry
        self.factory = factory
        self.options = options
        self._runtime: dict[tuple[str, tuple[str, ...] | None], dict[str, SchemaSpec]] = {}
        """What `ensure` was given, by owner and databases, each item by its identity: a later one replaces it."""
        self._lock = concurrency.Lock()
        """Serializes `enforce`, `recreate_all` and `ensure`, which read and write the runtime items and the markers."""

    async def enforce(self) -> dict[str, list[EnsureResult[Any]]]:
        """Ensures every catalog on its databases: indexes before constraints. Returns the results, by database."""
        return await self._apply(False)

    async def recreate_all(self) -> dict[str, list[EnsureResult[Any]]]:
        """Drops and makes every declared item, and every item given to `ensure`. Destructive."""
        return await self._apply(True)

    async def ensure(self, owner: str, *specs: SchemaSpec) -> dict[str, list[EnsureResult[Any]]]:
        """
        Ensures schema known only at runtime under `owner` (DEFAULT_SCOPE is the unnamed catalogs' owner), on every
        graph database. See `ensure_catalog`.
        """
        return await self.ensure_catalog(SchemaCatalog(tuple(specs), name=owner))

    async def ensure_catalog(self, catalog: SchemaCatalog) -> dict[str, list[EnsureResult[Any]]]:
        """
        Ensures schema known only at runtime, a vector index whose dimensions come with the embedding model or an index
        for each label a tenant declares, under the catalog's owner and on its databases. Returns the results of the
        items given, by database.

        The items join what the owner declared, for the life of this manager, and are applied as `enforce` applies the
        owner's: under the options' `mode`, `recreate_on_drift`, `violation_sample_size` and `replace_blocking_index`,
        and recorded in the owner's marker. Only the items given are applied. Ensuring an item again with the same
        identity (kind, label, properties) replaces it; two of one identity given at once are merged as a catalog's
        are. A later `enforce` or `recreate_all` applies them with the rest, and they count as the owner's when another
        owner declares one too.
        Every database is planned first: a declaration that conflicts with another owner's, with the owner's own, or
        with another given at once, raises, and nothing is kept or applied. So does a vector index over a fragment's
        field that has no `VectorIndex`, as in a catalog.

        The marker records them as ensured at runtime, so an `enforce` by a manager made later (the next process) keeps
        them in the inventory, not as orphans, until they are ensured again. When that manager recreates the owner (a
        version change, `recreate_on_startup` or `recreate_all`), it marks them, and the next `ensure` of one recreates
        it. A version change is left to `enforce`, and `recreate_on_startup` doesn't apply; the catalog can't carry a
        version.
        """
        if catalog.version is not None:
            raise DrivineError(
                f"Schema ensured at runtime cannot carry a version ({catalog.version}): an owner's version is declared "
                "by its catalogs, with SchemaCatalog.with_version."
            )
        if not catalog.items:
            logger.info("Nothing to ensure for owner '%s': no items given", catalog.name)
            return {}
        async with self._lock:
            targets = self._targets(catalog)
            # The items given are merged as a catalog's are, before one can replace another of its identity: two that
            # differ raise, one with engine options wins over one without, and identical ones are kept once. Their
            # merge is the same on every database, or with none registered, as drivine4j builds a catalog: a conflict
            # names no database.
            items = tuple(self._merged(None, [catalog]).values())
            candidate = self._with_runtime(catalog, items)
            declared = self._catalogs_by_database(self.options.catalogs)
            ensured = self._catalogs_by_database(_runtime_catalogs(candidate))
            # Every database is planned before anything is kept or applied, so a conflict leaves all as they were.
            plans = {
                database: self._plan(database, declared.get(database, []), ensured.get(database, []))
                for database in targets
            }
            # After planning, before anything is kept: a vector index over a fragment's field without VectorIndex is
            # refused, as drivine4j does.
            vector_annotation_check.register(catalog.items)
            self._runtime = candidate
            given = {inventory_key(it) for it in catalog.items}
            report: dict[str, list[EnsureResult[Any]]] = {}
            for database, plan in plans.items():
                report[database] = await self._apply_plan(database, plan, False, catalog.name, given)
            return report

    async def _apply(self, recreate: bool) -> dict[str, list[EnsureResult[Any]]]:
        async with self._lock:
            runtime = _runtime_catalogs(self._runtime)
            # Items ensured at runtime are something to apply, as drivine4j counts them.
            if not any(it.items for it in [*self.options.catalogs, *runtime]):
                self._log_nothing_to_apply()
                return {}
            # Before anything is applied: a vector index over a fragment's field without VectorIndex is refused.
            vector_annotation_check.register(spec for it in [*self.options.catalogs, *runtime] for spec in it.items)
            declared = self._catalogs_by_database(self.options.catalogs)
            ensured = self._catalogs_by_database(runtime)
            report: dict[str, list[EnsureResult[Any]]] = {}
            for database in dict.fromkeys([*declared, *ensured]):
                plan = self._plan(database, declared.get(database, []), ensured.get(database, []))
                report[database] = await self._apply_plan(database, plan, recreate, None, None)
            return report

    def _with_runtime(
        self, catalog: SchemaCatalog, items: Sequence[SchemaSpec]
    ) -> dict[tuple[str, tuple[str, ...] | None], dict[str, SchemaSpec]]:
        """
        The runtime items with the catalog's merged `items` added, each replacing the item of its identity, owner and
        databases.
        """
        copy = {key: dict(specs) for key, specs in self._runtime.items()}
        # The databases as a set, as drivine4j keeps a catalog's: the order they are named in is no part of the key.
        databases = None if catalog.databases is None else tuple(sorted(set(catalog.databases)))
        specs = copy.setdefault((catalog.name, databases), {})
        for spec in items:
            specs[_key_of(spec)] = spec
        return copy

    def _plan(self, database: str, declared: list[SchemaCatalog], ensured: list[SchemaCatalog]) -> _Plan:
        """Merges what each owner declares on the database, at start and at runtime alike; raises on a conflict."""
        catalogs = [*declared, *ensured]
        owners: dict[str, list[SchemaCatalog]] = {}
        for it in catalogs:
            owners.setdefault(it.name, []).append(it)
        runtime_only: dict[str, set[str]] = {}
        for it in ensured:
            runtime_only.setdefault(it.name, set()).update(inventory_key(spec) for spec in it.items)
        for owner, keys in runtime_only.items():
            keys -= {inventory_key(spec) for it in declared if it.name == owner for spec in it.items}
        return _Plan(self._merged(database, catalogs), owners, _co_owned_keys(owners), runtime_only)

    async def _apply_plan(
        self, database: str, plan: _Plan, recreate: bool, owner: str | None, only: set[str] | None
    ) -> list[EnsureResult[Any]]:
        """Applies the plan's owners, or only `owner`; with `only`, just the items of those inventory keys."""
        gom = self.factory.get(database)
        results: list[EnsureResult[Any]] = []
        for name, own in plan.owners.items():
            if owner is not None and name != owner:
                continue
            others = {
                inventory_key(spec)
                for other, cats in plan.owners.items()
                if other != name
                for c in cats
                for spec in c.items
            }
            results.extend(
                await self._apply_owner(
                    gom, database, name, own, plan, plan.runtime_only.get(name, set()), others, recreate, only
                )
            )
        return results

    def _log_nothing_to_apply(self) -> None:
        """
        Says why there is nothing to apply, so that an application with no schema, one that never passed its catalog
        and one whose catalogs all came out empty can be told apart. Both lines name the snapshot, a catalog made after
        the manager isn't seen, and say where schema known later goes.
        """
        catalogs = self.options.catalogs
        if not catalogs:
            logger.info(
                "Schema management found no SchemaCatalog; nothing to apply. Catalogs are read once, when the "
                "SchemaManager is built; schema known later is given to SchemaManager.ensure."
            )
            return
        counts: dict[str, int] = {}
        for it in catalogs:
            owner = "unnamed" if it.name == DEFAULT_SCOPE else f"'{it.name}'"
            counts[owner] = counts.get(owner, 0) + 1
        logger.info(
            "Schema management found %s SchemaCatalog(s), all empty: %s; nothing to apply. Catalogs are read once, "
            "when the SchemaManager is built; schema known later is given to SchemaManager.ensure.",
            len(catalogs),
            ", ".join(owner if count == 1 else f"{owner} ({count})" for owner, count in counts.items()),
        )

    async def _apply_owner(
        self,
        gom: StatelessGraphObjectManager,
        database: str,
        owner: str,
        catalogs: list[SchemaCatalog],
        plan: _Plan,
        runtime_only: set[str],
        declared_by_others: set[str],
        forced: bool,
        only: set[str] | None,
    ) -> list[EnsureResult[Any]]:
        """
        Applies one owner's items: recreated when forced, when `recreate_on_startup`, or when the owner's version
        changed since it last applied (a first version is adopted, not acted on); an item another owner declares too is
        never recreated by one owner's version. Items the owner applied before and no longer declares (orphans) are
        reported, never dropped. The owner's version and inventory are recorded only when the apply succeeds, so a
        failed one is tried again on the next enforce.

        `only`, from `ensure`, is the keys of the items given: just those are applied, the version stays as recorded,
        and `recreate_on_startup` doesn't apply. Runtime items an earlier manager recorded and this one hasn't been
        given are carried in the inventory, and marked stale when the owner is recreated without them.
        """
        ensuring = only is not None
        versions = sorted({it.version for it in catalogs if it.version is not None})
        version = "|".join(versions) if versions else None
        pm = gom.persistence_manager
        if version is not None:
            await self._ensure_singleton(gom, owner)
        # ensure records the version it finds, so a change it doesn't act on is left to enforce.
        stored = (
            await pm.maybe_get_one(gom.backend.schema_marker("version").spec({"scope": owner}))
            if version is not None or ensuring
            else None
        )
        version_changed = not ensuring and version is not None and stored is not None and stored != version
        recreate_owner = version_changed or (not ensuring and self.options.recreate_on_startup)
        own = [plan.items[key] for key in dict.fromkeys(_key_of(spec) for it in catalogs for spec in it.items)]
        declared = [inventory_key(it) for it in own]
        recorded = read_inventory(
            gom.backend.marker_items(await pm.maybe_get_one(gom.backend.schema_marker("items").spec({"scope": owner})))
        )
        # Runtime items an earlier manager ensured and this one hasn't been given: kept, not orphans, unless an item
        # declared now has the same identity under another name (a rename), which makes the recorded one an orphan.
        identities = [inventory_key(it).removesuffix(item_name(it)) for it in own]
        carried = [
            key
            for key in (recorded.runtime if recorded is not None else ())
            if key not in declared and not any(key.startswith(it) for it in identities)
        ]
        stale: set[str] = set(recorded.stale) if recorded is not None else set()
        self._report_orphans(database, owner, recorded, {*declared, *carried}, plan.co_owned)
        applied = own if only is None else [it for it in own if inventory_key(it) in only]
        ordered = [*(it for it in applied if it.kind != "UNIQUE"), *(it for it in applied if it.kind == "UNIQUE")]
        results: list[EnsureResult[Any]] = []
        for spec in ordered:
            # A runtime item marked stale missed its owner's recreate, and is recreated now.
            key = inventory_key(spec)
            recreate = forced or ((recreate_owner or key in stale) and key not in plan.co_owned)
            results.append(await self._ensure(gom, spec, recreate))
        # A carried item has no spec here to be recreated from: it is marked, and recreated when next ensured. A
        # version change spares one another owner declares, as it spares a co-owned item. A runtime item not applied
        # this time keeps its mark.
        keys = {inventory_key(it) for it in applied}
        stale_after = [
            *(it for it in stale if it in runtime_only and it not in keys),
            *(it for it in carried if it in stale or forced or (recreate_owner and it not in declared_by_others)),
        ]
        items = inventory_entries([*declared, *carried], {*runtime_only, *carried}, stale_after)
        await pm.execute(
            gom.backend.record_schema_marker().spec(
                {"scope": owner, "version": stored if ensuring else version, "items": items}
            )
        )
        return results

    async def _ensure_singleton(self, gom: StatelessGraphObjectManager, owner: str) -> None:
        """
        Keeps one marker for each owner: drops all but the newest a race left, and backs the marker's key with a
        uniqueness constraint, so two processes enforcing at once can't each make one.
        """
        await gom.persistence_manager.execute(gom.backend.prune_schema_markers().spec({"scope": owner}))
        # A plain index on the marker's key (a hand-made fix, say) is in the way on Neo4j. The label is Drivine's, so
        # the constraint replaces it.
        await gom.constraints.ensure(uniqueness_constraint(MARKER_LABEL, "scope"), replace_blocking_index=True)

    @staticmethod
    def _report_orphans(
        database: str, owner: str, recorded: RecordedInventory | None, kept: set[str], co_owned: set[str]
    ) -> None:
        if recorded is None:
            return
        orphans = [it for it in recorded.keys if it not in kept and it not in co_owned]
        if orphans:
            logger.warning(
                "Owner '%s' on %s applied %s item(s) it no longer declares: %s. They are left in place; drop them, or "
                "recreate, if unwanted.",
                owner,
                database,
                len(orphans),
                ", ".join(orphans),
            )

    async def _ensure(self, gom: StatelessGraphObjectManager, spec: SchemaSpec, recreate: bool) -> EnsureResult[Any]:
        if isinstance(spec, UniquenessConstraintSpec):
            if recreate:
                await gom.constraints.drop(spec)
            return self._judge(
                await gom.constraints.ensure(
                    spec, self.options.violation_sample_size, self.options.replace_blocking_index
                )
            )
        if recreate:
            return await gom.indexes.recreate(spec)
        result = await gom.indexes.ensure(spec)
        if result.status == "DRIFT" and self.options.recreate_on_drift:
            return await gom.indexes.recreate(spec)
        return self._judge(result)

    def _judge(self, result: EnsureResult[Any]) -> EnsureResult[Any]:
        if result.status in ("DRIFT", "VIOLATION", "BLOCKED_BY_INDEX"):
            spec = result.spec
            message = f"{result.status} for the {spec.kind} on {spec.label}({', '.join(spec.properties)})"
            if result.index is not None:
                message += (
                    f": the index {result.index.name} is in the way of it. Drop the index, or replace it with the "
                    "constraint: replace_blocking_index=True"
                )
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

    def _catalogs_by_database(self, catalogs: Sequence[SchemaCatalog]) -> dict[str, list[SchemaCatalog]]:
        """The catalogs that apply to each graph database. A named database that is unknown or not a graph fails."""
        result: dict[str, list[SchemaCatalog]] = {}
        for catalog in catalogs:
            for name in self._targets(catalog):
                result.setdefault(name, []).append(catalog)
        return result

    def _targets(self, catalog: SchemaCatalog) -> list[str]:
        """The databases a catalog applies to: those it names, each a registered graph, or every graph."""
        if catalog.databases is None:
            return [it.name for it in self.registry.providers if it.type in GRAPH_TYPES]
        for name in catalog.databases:
            provider = self.registry.connection_provider(name)
            if provider is None or provider.type not in GRAPH_TYPES:
                raise DrivineError(f"A schema catalog names {name}, which is not a registered graph database.")
        return list(catalog.databases)

    def _merged(self, database: str | None, catalogs: list[SchemaCatalog]) -> dict[str, SchemaSpec]:
        """
        The items of a database's catalogs, each once; with no database (catalogs merged before any is applied), a
        conflict names none. Two declarations of one item must agree on everything portable;
        where one of them carries engine options it wins whole, and two that carry different engine options conflict.
        """
        items: dict[str, SchemaSpec] = {}
        owners: dict[str, str] = {}
        for catalog in catalogs:
            for spec in catalog.items:
                key = _key_of(spec)
                held = items.get(key)
                if held is None:
                    items[key] = spec
                    owners[key] = catalog.name
                else:
                    items[key] = self._reconciled(database, held, spec, owners[key], catalog.name)
                    if items[key] is not held:  # its engine options won: a later conflict is with this one
                        owners[key] = catalog.name
        return items

    @staticmethod
    def _reconciled(database: str | None, held: SchemaSpec, spec: SchemaSpec, held_by: str, owner: str) -> SchemaSpec:
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
        by = (
            f"Two schema catalogs of '{owner}'" if held_by == owner else f"Schema catalogs of '{held_by}' and '{owner}'"
        )
        on = "" if database is None else f" on {database}"
        raise DrivineError(
            f"{by} declare the {spec.kind} on {spec.label}({', '.join(spec.properties)}){on} differently: "
            f"{held} and {spec}."
        )


def _runtime_catalogs(
    runtime: Mapping[tuple[str, tuple[str, ...] | None], Mapping[str, SchemaSpec]],
) -> list[SchemaCatalog]:
    """What `ensure` was given, as a catalog for each owner and databases."""
    return [
        SchemaCatalog(tuple(specs.values()), databases=databases, name=owner)
        for (owner, databases), specs in runtime.items()
    ]


def _co_owned_keys(owners: Mapping[str, list[SchemaCatalog]]) -> set[str]:
    """The inventory keys more than one owner declares."""
    counts: dict[str, int] = {}
    for catalogs in owners.values():
        for key in {inventory_key(spec) for it in catalogs for spec in it.items}:
            counts[key] = counts.get(key, 0) + 1
    return {key for key, count in counts.items() if count > 1}
