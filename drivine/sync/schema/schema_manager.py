# Written by scripts/unasync.py from drivine/schema/schema_manager.py:
# change that, and run the script again.
from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from drivine.backend.sql.sql_catalog import CATALOG_KINDS
from drivine.connection.database_type import DatabaseType
from drivine.drivine_error import DrivineError
from drivine.query.query_specification import QuerySpecification
from drivine.query.statement import cypher_statement
from drivine.schema import node_id_index_check, vector_annotation_check
from drivine.schema.ensure_result import EnsureResult
from drivine.schema.recorded_inventory import RecordedInventory, inventory_entries, read_inventory
from drivine.schema.schema_catalog import DEFAULT_SCOPE, SchemaCatalog
from drivine.schema.schema_plan import ItemPolicy, Recreate, ensure_result, module_spec, plan_request, plan_spec
from drivine.schema.schema_specs import SchemaSpec, item_name, made_as, uniqueness_constraint
from drivine.schema.uniqueness_constraint_spec import UniquenessConstraintSpec
from drivine.schema.vector_index_spec import VectorIndexSpec
from drivine.sync import concurrency
from drivine.sync.connection.database_registry import DatabaseRegistry
from drivine.sync.manager.graph_object_manager_factory import GraphObjectManagerFactory
from drivine.sync.manager.persistence_manager import PersistenceManager
from drivine.sync.manager.stateless_graph_object_manager import StatelessGraphObjectManager
from drivine.sync.schema.index_manager import IndexNotBuilt
from drivine.sync.schema.schema_options import SchemaOptions
from drivine.sync.schema.schema_plan_driver import run_plan

if TYPE_CHECKING:
    from drivine.sync.schema.planned_managers import SchemaPlanning

logger = logging.getLogger("drivine.schema")

GRAPH_TYPES = (
    DatabaseType.NEO4J,
    DatabaseType.MEMGRAPH,
    DatabaseType.FALKORDB,
    DatabaseType.ORACLE,
    DatabaseType.POSTGRES,
    DatabaseType.SQLITE,
)

MARKER_LABEL = "_DrivineSchema"
"""The label of the schema markers: drivine4j's and TypeScript's, so all read one."""


def _owner_of(name: str) -> str | None:
    """An owner as the planner names one: the unnamed owner, DEFAULT_SCOPE, is None, as drivine4j's unnamed one is."""
    return None if name == DEFAULT_SCOPE else name


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
    After `enforce` or `recreate_all` applied the catalogs on a database, a fragment whose NodeId no index there serves
    is warned of, once per database: see node_id_index_check.
    """

    def __init__(self, registry: DatabaseRegistry, factory: GraphObjectManagerFactory, options: SchemaOptions) -> None:
        self.registry = registry
        self.factory = factory
        self.options = (
            options
            if options.index_node_ids
            else dataclasses.replace(options, catalogs=[it.without_node_id_indexes() for it in options.catalogs])
        )
        self._runtime: dict[tuple[str, tuple[str, ...] | None], dict[str, SchemaSpec]] = {}
        """What `ensure` was given, by owner and databases, each item by its identity: a later one replaces it."""
        self._lock = concurrency.Lock()
        """Serializes `enforce`, `recreate_all` and `ensure`, which read and write the runtime items and the markers."""
        # A manager with no item to apply applies none, and lists nothing: its fragments are warned of as where there
        # is no manager.
        if any(it.items for it in self.options.catalogs):
            node_id_index_check.managed()

    def enforce(self) -> dict[str, list[EnsureResult[Any]]]:
        """Ensures every catalog on its databases: indexes before constraints. Returns the results, by database."""
        return self._apply(False)

    def recreate_all(self) -> dict[str, list[EnsureResult[Any]]]:
        """Drops and makes every declared item, and every item given to `ensure`. Destructive."""
        return self._apply(True)

    def ensure(self, owner: str, *specs: SchemaSpec) -> dict[str, list[EnsureResult[Any]]]:
        """
        Ensures schema known only at runtime under `owner` (DEFAULT_SCOPE is the unnamed catalogs' owner), on every
        graph database, built as `SchemaCatalog.of` builds a catalog's items: indexes before constraints, as drivine4j
        gives them. See `ensure_catalog`.
        """
        return self.ensure_catalog(SchemaCatalog.of(*specs).named(owner))

    def ensure_catalog(self, catalog: SchemaCatalog) -> dict[str, list[EnsureResult[Any]]]:
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
        with self._lock:
            targets = self._targets(catalog)
            # The items given are merged as a catalog's are, before one can replace another of its identity: two that
            # differ raise, one with engine options wins over one without, and identical ones are kept once. Their
            # merge is the same on every database, or with none registered, as drivine4j builds a catalog: a conflict
            # names no database.
            items = tuple(self._merged(None, [catalog]).values())
            candidate = self._with_runtime(catalog, items)
            ensured = self._catalogs_by_database(_runtime_catalogs(candidate))
            declared = _beside(self._catalogs_by_database(self.options.catalogs), ensured)
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
            operation = {"type": "ensure", "owner": _owner_of(catalog.name), "items": [module_spec(it) for it in items]}
            report: dict[str, list[EnsureResult[Any]]] = {}
            for database, plan in plans.items():
                planned = self._planned(database, operation)
                report[database] = (
                    planned if planned is not None else self._apply_plan(database, plan, False, catalog.name, given)
                )
                # What an enforce listed is kept current, so a fragment first used after this isn't warned of. Its
                # labels are named on the database from then on, so a fragment under one is checked there too.
                registered = self._registered(database)
                if node_id_index_check.listed_on(registered):
                    node_id_index_check.catalogued_too(
                        registered, [*(it.label for it in catalog.items), *catalog.fragment_labels]
                    )
                    self._list_for_node_ids(database)
            return report

    def remove(self, owner: str, *specs: SchemaSpec) -> None:
        """
        Takes items back from what `owner` declares, on every graph database, the specs as `SchemaCatalog.of` holds
        them, as drivine4j's remove does. See `remove_catalog`.
        """
        self.remove_catalog(SchemaCatalog.of(*specs).named(owner))

    def remove_catalog(self, catalog: SchemaCatalog) -> None:
        """
        Takes the catalog's items back from what its owner declares, on its databases: the counterpart of
        `ensure_catalog`. Each item is found by its identity (kind, label and properties, in any order), whatever its
        name.

        An item `ensure` gave the owner is forgotten, so that a later `enforce` doesn't make it again. On each database,
        the item is then dropped, unless a startup catalog (of any owner) or another owner's runtime items declare it,
        or another owner's marker there records it (declared or ensured at runtime, by whichever manager or process):
        it is then left in place, with a warning naming who declares it. Either way the owner's marker no longer records
        it, so it is no orphan of the owner. An item that isn't there is dropped as nothing, and forgotten all the
        same. The catalog can't carry a version. drivine4j's `SchemaManager.remove`.
        """
        if catalog.version is not None:
            raise DrivineError(
                f"Schema removed at runtime cannot carry a version ({catalog.version}): an owner's version is declared "
                "by its catalogs, with SchemaCatalog.with_version."
            )
        if not catalog.items:
            return
        with self._lock:
            identities = {_key_of(it) for it in catalog.items}
            databases = self._databases_of(catalog)
            for (owner, targets), specs in self._runtime.items():
                if owner == catalog.name and (databases is None or targets == databases):
                    for identity in identities:
                        specs.pop(identity, None)
            self._runtime = {key: specs for key, specs in self._runtime.items() if specs}
            operation = {
                "type": "remove",
                "owner": _owner_of(catalog.name),
                "items": [module_spec(it) for it in catalog.items],
            }
            for database in self._targets(catalog):
                if self._planned(database, operation) is None:
                    self._remove_from(database, catalog)

    def ensured(self, owner: str) -> list[SchemaSpec]:
        """What `ensure` has given the owner, on any database."""
        return [spec for (name, _), specs in self._runtime.items() if name == owner for spec in specs.values()]

    def _remove_from(self, database: str, catalog: SchemaCatalog) -> None:
        """`remove_catalog`, on one database: drops what no one else declares there, and takes the items out of the
        owner's marker."""
        owner = catalog.name
        gom = self.factory.get(database)
        pm = gom.persistence_manager
        runtime = [it for it in _runtime_catalogs(self._runtime) if database in self._targets(it)]
        # The startup catalogs as they are applied there: a NodeId's default another item serves declares nothing.
        on_database = _beside(
            {database: [it for it in self.options.catalogs if database in self._targets(it)]}, {database: runtime}
        )[database]
        elsewhere = [*(it for it in on_database if it.items), *(it for it in runtime if it.name != owner)]
        # What other owners' markers record, declared or ensured at runtime: an owner this manager wasn't given, such
        # as an application's, whose catalogs are another manager's or another process's.
        recorded_elsewhere: dict[str, tuple[str, ...]] = {}
        for scope in self._marker_scopes(pm):
            if scope != owner:
                held = self._recorded(gom, scope)
                recorded_elsewhere[scope] = held.keys if held is not None else ()
        dropped = 0
        kept = 0
        for spec in catalog.items:
            identity = _key_of(spec)
            declaring = list(
                dict.fromkeys(
                    [
                        *(it.name for it in elsewhere if any(_key_of(other) == identity for other in it.items)),
                        *(
                            scope
                            for scope, keys in recorded_elsewhere.items()
                            if any(_records_identity(key, spec) for key in keys)
                        ),
                    ]
                )
            )
            if declaring:
                logger.warning(
                    "%s on %s(%s) is removed from owner '%s' on %s and left in place, as %s declare(s) it",
                    spec.kind,
                    spec.label,
                    ", ".join(spec.properties),
                    owner,
                    database,
                    ", ".join(f"'{it}'" for it in declaring),
                )
                kept += 1
                continue
            if isinstance(spec, UniquenessConstraintSpec):
                gom.constraints.drop(spec)
            else:
                gom.indexes.drop(spec)
            dropped += 1
        recorded = self._recorded(gom, owner)
        if recorded is not None:
            gone = {key for key in recorded.keys if any(_records_identity(key, it) for it in catalog.items)}
            if gone:
                version = pm.maybe_get_one(gom.backend.schema_marker("version").spec({"scope": owner}))
                items = inventory_entries(
                    [it for it in recorded.keys if it not in gone],
                    [it for it in recorded.runtime if it not in gone],
                    [it for it in recorded.stale if it not in gone],
                )
                pm.execute(
                    gom.backend.record_schema_marker().spec({"scope": owner, "version": version, "items": items})
                )
        logger.info(
            "Schema for owner '%s' on %s: %s removed, %s dropped, %s left in place",
            owner,
            database,
            len(catalog.items),
            dropped,
            kept,
        )

    def _declared_on(
        self, database: str, planning: SchemaPlanning
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """
        What a plan on `database` is given of what is declared, as the module's JSON writes it: the startup catalogs
        that apply there, each its owner, version and items, and the items `ensure` gave that hold there, a catalog for
        each owner. The Cypher spec's declarations.yaml pins these, as drivine4j's declarations give them
        (liberation-data/drivine-cypher-spec#135).
        """
        ensured = self._catalogs_by_database(_runtime_catalogs(self._runtime)).get(database, [])
        on_database = _beside(
            {database: [it for it in self.options.catalogs if database in self._targets(it)]}, {database: ensured}
        )
        catalogs = [
            {"owner": _owner_of(it.name), "version": it.version, "items": [module_spec(spec) for spec in it.items]}
            for it in on_database[database]
        ]
        runtime = [
            {
                "owner": _owner_of(owner),
                "databases": None if databases is None else list(databases),
                "items": [module_spec(spec) for spec in specs.values()],
            }
            for (owner, databases), specs in self._runtime.items()
        ]
        return catalogs, planning.plan.runtime_on(database, runtime)

    def _planned(self, database: str, operation: Mapping[str, Any]) -> list[EnsureResult[Any]] | None:
        """
        `operation` on `database`, where it is a SQL store, as the shared module's schema plan applies it: the startup
        catalogs that apply there and the items `ensure` gave, each owner's markers, drift, violations and indexes not
        built, and what is logged, as drivine4j applies it (see schema_plan). The results of each owner's items, in
        order; None on a graph engine, whose schema this manager plans itself (liberation-data/drivine-python#515).
        """
        planning = self.factory.get(database).schema_planning()
        if planning is None:
            return None
        options = self.options
        catalogs, runtime = self._declared_on(database, planning)
        request = plan_request(
            database,
            operation,
            ItemPolicy(
                mode=options.mode,
                violation_sample_size=options.violation_sample_size,
                replace_blocking_index=options.replace_blocking_index,
                build_timeout_millis=int(options.index_build_timeout * 1000),
            ),
            catalogs,
            runtime,
            Recreate(on_drift=options.recreate_on_drift, on_startup=options.recreate_on_startup),
        )
        refusal = planning.plan.validate(request)
        if refusal is not None:
            raise DrivineError(refusal)
        report = run_plan(planning.plan, request, planning.host)
        return [
            ensure_result(it["result"], plan_spec(it["spec"])) for owner in report["owners"] for it in owner["results"]
        ]

    @staticmethod
    def _recorded(gom: StatelessGraphObjectManager, owner: str) -> RecordedInventory | None:
        """What the owner's marker records."""
        stored = gom.persistence_manager.maybe_get_one(gom.backend.schema_marker("items").spec({"scope": owner}))
        return read_inventory(gom.backend.marker_items(stored))

    @staticmethod
    def _marker_scopes(pm: PersistenceManager) -> list[str]:
        """
        The owners whose markers the database holds, whichever manager or process recorded them: read as Cypher, which
        every store with a marker runs (the SQL stores compile it), the marker being a node of the graph on each.
        """
        found = pm.query(
            QuerySpecification[Any](cypher_statement(f"MATCH (m:`{MARKER_LABEL}`) RETURN DISTINCT m.scope"))
        )
        return [str(it) for it in found if it is not None]

    def _apply(self, recreate: bool) -> dict[str, list[EnsureResult[Any]]]:
        with self._lock:
            runtime = _runtime_catalogs(self._runtime)
            # Items ensured at runtime are something to apply, as drivine4j counts them.
            if not any(it.items for it in [*self.options.catalogs, *runtime]):
                self._log_nothing_to_apply()
                return {}
            # Before anything is applied: a vector index over a fragment's field without VectorIndex is refused.
            vector_annotation_check.register(spec for it in [*self.options.catalogs, *runtime] for spec in it.items)
            ensured = self._catalogs_by_database(runtime)
            on_database = self._catalogs_by_database(self.options.catalogs)
            declared = _beside(on_database, ensured)
            # Before any is listed: a fragment is checked on the databases whose catalogs name its labels.
            node_id_index_check.catalogued(self._catalogued_on(on_database, ensured))
            report: dict[str, list[EnsureResult[Any]]] = {}
            for database in dict.fromkeys([*declared, *ensured]):
                plan = self._plan(database, declared.get(database, []), ensured.get(database, []))
                operation = {"type": "recreateAll" if recreate else "enforce", "owners": {"all": True}}
                planned = self._planned(database, operation)
                report[database] = (
                    planned if planned is not None else self._apply_plan(database, plan, recreate, None, None)
                )
            # Each database once, after all its catalogs are applied: one applied as `default` and by its name is listed
            # only once both are, so an index the second makes isn't taken for missing.
            for database in dict.fromkeys(self._registered(it) for it in report):
                self._list_for_node_ids(database)
            return report

    def _catalogued_on(
        self, declared: Mapping[str, list[SchemaCatalog]], ensured: Mapping[str, list[SchemaCatalog]]
    ) -> dict[str, set[str]]:
        """
        The labels the catalogs on each database, declared or ensured at runtime, have items under or were read from,
        by the database's registered name: for node_id_index_check to check a fragment where it belongs. A catalog
        read from a fragment with its default index off declares nothing, and names its labels all the same.
        """
        labels: dict[str, set[str]] = {}
        for database in dict.fromkeys([*declared, *ensured]):
            named = labels.setdefault(self._registered(database), set())
            for catalog in [*declared.get(database, []), *ensured.get(database, [])]:
                named.update(it.label for it in catalog.items)
                named.update(catalog.fragment_labels)
        return labels

    def _list_for_node_ids(self, database: str) -> None:
        """
        What serves a fragment's NodeId on `database` once the catalogs are applied, its range indexes and uniqueness
        constraints, for node_id_index_check to warn of a fragment none serves. Read outside any transaction, as the
        managers read it. A listing that fails is passed over: it only warns.
        """
        try:
            gom = self.factory.get(database)
            indexes = gom.indexes
            ranges = indexes.list("RANGE")
            uniques = gom.constraints.list()
            ranges_per_property = bool(indexes.grammar.ranges_per_property)
        except Exception as error:
            logger.debug("Could not list the schema of '%s' to check fragments' NodeIds: %s", database, error)
            return
        node_id_index_check.listed(self._registered(database), ranges, uniques, ranges_per_property)

    def _registered(self, database: str) -> str:
        """The name `database` is registered under: `default` is the first registered database's."""
        provider = self.registry.connection_provider(database)
        return provider.name if provider is not None else database

    def _with_runtime(
        self, catalog: SchemaCatalog, items: Sequence[SchemaSpec]
    ) -> dict[tuple[str, tuple[str, ...] | None], dict[str, SchemaSpec]]:
        """
        The runtime items with the catalog's merged `items` added, each replacing the item of its identity, owner and
        databases.
        """
        copy = {key: dict(specs) for key, specs in self._runtime.items()}
        specs = copy.setdefault((catalog.name, self._databases_of(catalog)), {})
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

    def _apply_plan(
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
                self._apply_owner(
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

    def _apply_owner(
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
        failed one is tried again on the next enforce. An index whose build failed or did not end in time is reported
        once the other items are applied: raised, every such index listed, under FAIL_FAST, and logged under WARN.

        `only`, from `ensure`, is the keys of the items given: just those are applied, the version stays as recorded,
        and `recreate_on_startup` doesn't apply. Runtime items an earlier manager recorded and this one hasn't been
        given are carried in the inventory, and marked stale when the owner is recreated without them.
        """
        ensuring = only is not None
        versions = sorted({it.version for it in catalogs if it.version is not None})
        version = "|".join(versions) if versions else None
        pm = gom.persistence_manager
        if version is not None:
            self._ensure_singleton(gom, owner)
        # ensure records the version it finds, so a change it doesn't act on is left to enforce.
        stored = (
            pm.maybe_get_one(gom.backend.schema_marker("version").spec({"scope": owner}))
            if version is not None or ensuring
            else None
        )
        version_changed = not ensuring and version is not None and stored is not None and stored != version
        recreate_owner = version_changed or (not ensuring and self.options.recreate_on_startup)
        own = [plan.items[key] for key in dict.fromkeys(_key_of(spec) for it in catalogs for spec in it.items)]
        declared = [inventory_key(it) for it in own]
        recorded = read_inventory(
            gom.backend.marker_items(pm.maybe_get_one(gom.backend.schema_marker("items").spec({"scope": owner})))
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
        unbuilt: list[IndexNotBuilt] = []
        for spec in ordered:
            # A runtime item marked stale missed its owner's recreate, and is recreated now.
            key = inventory_key(spec)
            recreate = forced or ((recreate_owner or key in stale) and key not in plan.co_owned)
            results.append(self._ensure(gom, spec, recreate, unbuilt))
        problems = [_described(it) for it in unbuilt]
        if problems and self.options.mode != "WARN":
            # After the owner's other items are applied, and before its marker is written, so the next apply waits
            # again.
            raise DrivineError(
                f"Schema application for owner '{owner}' on database '{database}' found problems:\n"
                + "\n".join(problems)
                + "\nResolve them, make SchemaOptions(index_build_timeout=...) longer for an index still being built, "
                'or set mode="WARN" to continue with warnings.'
            ) from unbuilt[0]
        # A carried item has no spec here to be recreated from: it is marked, and recreated when next ensured. A
        # version change spares one another owner declares, as it spares a co-owned item. A runtime item not applied
        # this time keeps its mark.
        keys = {inventory_key(it) for it in applied}
        stale_after = [
            *(it for it in stale if it in runtime_only and it not in keys),
            *(it for it in carried if it in stale or forced or (recreate_owner and it not in declared_by_others)),
        ]
        items = inventory_entries([*declared, *carried], {*runtime_only, *carried}, stale_after)
        pm.execute(
            gom.backend.record_schema_marker().spec(
                {"scope": owner, "version": stored if ensuring else version, "items": items}
            )
        )
        for problem in problems:
            logger.warning(problem)
        return results

    def _ensure_singleton(self, gom: StatelessGraphObjectManager, owner: str) -> None:
        """
        Keeps one marker for each owner: drops all but the newest a race left, and backs the marker's key with a
        uniqueness constraint, so two processes enforcing at once can't each make one.
        """
        gom.persistence_manager.execute(gom.backend.prune_schema_markers().spec({"scope": owner}))
        # A plain index on the marker's key (a hand-made fix, say) is in the way on Neo4j. The label is Drivine's, so
        # the constraint replaces it.
        gom.constraints.ensure(uniqueness_constraint(MARKER_LABEL, "scope"), replace_blocking_index=True)

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

    def _ensure(
        self, gom: StatelessGraphObjectManager, spec: SchemaSpec, recreate: bool, unbuilt: list[IndexNotBuilt]
    ) -> EnsureResult[Any]:
        """
        Applies the item, adding an index whose build failed or did not end in time to `unbuilt`, which needs
        attention as a drift does, and giving what was done: raised under FAIL_FAST once the owner's other items are
        applied, and logged under WARN.
        """
        try:
            return self._ensure_built(gom, spec, recreate)
        except IndexNotBuilt as not_built:
            unbuilt.append(not_built)
            return not_built.result

    def _ensure_built(self, gom: StatelessGraphObjectManager, spec: SchemaSpec, recreate: bool) -> EnsureResult[Any]:
        timeout = self.options.index_build_timeout
        if isinstance(spec, UniquenessConstraintSpec):
            constraints = gom.constraints
            constraints.build_timeout = timeout
            make = constraints.recreate if recreate else constraints.ensure
            return self._judge(make(spec, self.options.violation_sample_size, self.options.replace_blocking_index))
        indexes = gom.indexes
        indexes.build_timeout = timeout
        if recreate:
            return indexes.recreate(spec)
        result = indexes.ensure(spec)
        if result.status == "DRIFT" and self.options.recreate_on_drift:
            return indexes.recreate(spec)
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
        """
        The databases a catalog applies to, each by the name it is registered under: those it names, each a registered
        graph, or every graph. `default` is the first registered database's name, so a catalog for it and one for every
        database are planned together there, as drivine4j's SchemaTargets resolves them (drivine4j#854, #595).
        """
        if catalog.databases is None:
            return [it.name for it in self.registry.providers if it.type in GRAPH_TYPES]
        targets: list[str] = []
        for name in catalog.databases:
            provider = self.registry.connection_provider(name)
            if provider is None or provider.type not in GRAPH_TYPES:
                raise DrivineError(f"A schema catalog names {name}, which is not a registered graph database.")
            targets.append(provider.name)
        return list(dict.fromkeys(targets))

    def _databases_of(self, catalog: SchemaCatalog) -> tuple[str, ...] | None:
        """
        The databases a catalog names, as its runtime items are kept by them: each by the name it is registered under
        (`default` the first registered database's), as a set, since the order they are named in is no part of the key;
        None for every database. drivine4j's `databasesOf`.
        """
        if catalog.databases is None:
            return None
        return tuple(sorted({self._registered(it) for it in catalog.databases}))

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


def _described(unbuilt: IndexNotBuilt) -> str:
    """An index not built, as SchemaManager reports it, as drivine4j's does."""
    if unbuilt.failed:
        return (
            f"  - BUILD FAILED: {unbuilt}. It is left in place, failed: drop or recreate it once the data it failed on "
            "is fixed"
        )
    return f"  - STILL BUILDING: {unbuilt}. It is left to be built, and waited for again on the next enforce"


def _beside(
    declared: Mapping[str, list[SchemaCatalog]], ensured: Mapping[str, list[SchemaCatalog]]
) -> dict[str, list[SchemaCatalog]]:
    """
    Each database's catalogs as they are applied there, beside what every catalog on it and every item ensured at
    runtime there declare: a NodeId's default range index another of them serves is left out, as one its own catalog's
    items serve is, as drivine4j's SchemaManager gives its catalogs (`itemsBeside`, drivine4j#827).
    """
    beside: dict[str, list[SchemaCatalog]] = {}
    for database, catalogs in declared.items():
        items = [
            *(spec for it in catalogs for spec in it.declared_items),
            *(spec for it in ensured.get(database, []) for spec in it.items),
        ]
        beside[database] = [it.beside(items) for it in catalogs]
    return beside


def _runtime_catalogs(
    runtime: Mapping[tuple[str, tuple[str, ...] | None], Mapping[str, SchemaSpec]],
) -> list[SchemaCatalog]:
    """What `ensure` was given, as a catalog for each owner and databases."""
    return [
        SchemaCatalog(tuple(specs.values()), databases=databases, name=owner)
        for (owner, databases), specs in runtime.items()
    ]


def _records_identity(key: str, spec: SchemaSpec) -> bool:
    """
    Whether the inventory key is of an item of the spec's identity, whatever its name: of its kind and label, and of its
    properties in any order, as `_key_of` compares them. An item of the properties in another order is the same item,
    so another owner's key of that order co-owns it, and the owner's own is forgotten with it.
    """
    made = made_as(spec)
    head = f"{CATALOG_KINDS[made.kind]}:{made.label}:"
    if not key.startswith(head):
        return False
    length = sum(len(it) for it in made.properties) + len(made.properties) - 1
    rest = key[len(head) :]
    if len(rest) <= length or rest[length] != ":":
        return False
    properties = rest[:length]
    return properties == ",".join(made.properties) or sorted(properties.split(",")) == sorted(made.properties)


def _co_owned_keys(owners: Mapping[str, list[SchemaCatalog]]) -> set[str]:
    """The inventory keys more than one owner declares."""
    counts: dict[str, int] = {}
    for catalogs in owners.values():
        for key in {inventory_key(spec) for it in catalogs for spec in it.items}:
            counts[key] = counts.get(key, 0) + 1
    return {key for key, count in counts.items() if count > 1}
