# Written by scripts/unasync.py from drivine/schema/schema_commands.py:
# change that, and run the script again.
"""
Runs Neo4j schema DDL on a SQL store, one database's, through Drivine's schema machinery. drivine4j's `SchemaCommands`,
ported.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from drivine.backend.sql.sql_catalog import CATALOG_KINDS, CatalogKind
from drivine.drivine_error import DrivineError
from drivine.schema import schema_ddl
from drivine.schema.full_text_index_spec import FullTextIndexSpec
from drivine.schema.range_index_spec import RangeIndexSpec
from drivine.schema.schema_catalog import SchemaCatalog
from drivine.schema.schema_ddl_result import (
    AwaitIndexes,
    Create,
    DdlSpec,
    Drop,
    NotDdl,
    Refused,
    Reserved,
    SchemaDdlResult,
    TextIndexSpec,
    default_name,
)
from drivine.schema.schema_item import SchemaItem
from drivine.schema.schema_specs import SchemaSpec
from drivine.schema.uniqueness_constraint_spec import UniquenessConstraintSpec
from drivine.schema.vector_index_spec import VectorIndexSpec
from drivine.sync import concurrency
from drivine.sync.schema.schema_manager import SchemaManager
from drivine.sync.schema.schema_options import SchemaOptions

if TYPE_CHECKING:
    from drivine.sync.connection.connection_provider import ConnectionProvider
    from drivine.sync.manager.persistence_manager_factory import PersistenceManagerFactory
    from drivine.sync.manager.stateless_graph_object_manager import StatelessGraphObjectManager

logger = logging.getLogger("drivine.schema")

OWNER = "ddl"
"""The owner the items DDL makes are ensured and recorded under: never an application's own."""


@dataclass(frozen=True)
class _Recorded:
    """An item the store holds, of its kind."""

    kind: CatalogKind
    item: SchemaItem


class SchemaCommands:
    """
    Runs Neo4j schema DDL on a SQL store, one database's, through Drivine's schema machinery: the statements a graph
    engine runs itself, which a Cypher statement on Postgres, Oracle or SQLite would otherwise be refused as.
    `schema_ddl.translate` reads the statement, and what it gives is applied as the schema API applies a spec, so the
    items are the ones the API makes, with the same names, expressions and inventory entries.

    - `CREATE` ensures the spec with `SchemaManager.ensure_catalog`, under the owner OWNER and never an application's
      own, whose declared items an ensure of its catalog would replace. The DDL's name (or, for DDL that names none, the
      API's name for its spec), where it isn't the name the item goes by, is kept as an alias of the item (see
      `IndexManager.alias`): so too over an item of the same properties in another order, which the API names by that
      order, and which the spec is then ensured as. Over an item of the same kind, label and properties that is there
      already, `IF NOT EXISTS` does nothing but add the alias, and without it the statement fails, as on Neo4j. A name
      that another item goes by, by the API's name or an alias, is refused, with or without `IF NOT EXISTS`.
    - `DROP` finds the item by the API's name, or else by an alias, and removes it with `SchemaManager.remove_catalog`.
      An item another owner's marker records, such as an application's, is forgotten by OWNER alone and left in place,
      with a warning, and no longer goes by the alias dropped. An item no one has by that name is no error with
      `IF EXISTS`, and fails without it.
    - `CALL db.awaitIndexes(…)` does nothing: an index on a SQL store is whole once it is made.
    - A constraint of Drivine's own nodes is ensured as Drivine ensures it, and recorded in no inventory.

    DDL is not run inside a transaction (`schema_ddl.refused_in_transaction`): Drivine's schema statements run on a
    connection of their own, committed as they go, which the caller's rollback would not undo.
    """

    def __init__(
        self, database: str, schema_manager: SchemaManager, manager: Callable[[], StatelessGraphObjectManager]
    ) -> None:
        """`database` is the database's registered name; `manager` gives its object manager when a command runs."""
        self.database = database
        self.schema_manager = schema_manager
        self._manager = manager
        self._lock = concurrency.Lock()
        """Serializes the commands, each of which reads what is there before it changes it."""

    @staticmethod
    def of(factory: PersistenceManagerFactory, name: str) -> SchemaCommands:
        """The commands of the database `name` of the factory's registry, ensured and removed through its managers."""
        from drivine.sync.manager.graph_object_manager_factory import GraphObjectManagerFactory

        provider = factory.registry.connection_provider(name)
        database = provider.name if provider is not None else name
        managers = GraphObjectManagerFactory(factory)
        schema = SchemaManager(factory.registry, managers, SchemaOptions())
        return SchemaCommands(database, schema, lambda: managers.get(database))

    @staticmethod
    def standalone(provider: ConnectionProvider) -> SchemaCommands:
        """The commands of the provider's database, for a manager made without a factory: a registry of it alone."""
        from drivine.sync.connection.database_registry import DatabaseRegistry
        from drivine.sync.manager.persistence_manager_factory import PersistenceManagerFactory

        registry = DatabaseRegistry()
        registry.register(provider)
        return SchemaCommands.of(PersistenceManagerFactory(registry), provider.name)

    def run(self, ddl: SchemaDdlResult) -> None:
        """Runs the schema DDL `schema_ddl.translate` read."""
        if isinstance(ddl, NotDdl):
            raise ValueError("Not schema DDL")
        if isinstance(ddl, Refused):
            raise DrivineError(ddl.message)
        if isinstance(ddl, AwaitIndexes):
            logger.debug(
                "db.awaitIndexes on '%s': an index on a SQL store is whole once made, so nothing is waited for",
                self.database,
            )
            return
        with self._lock:
            if isinstance(ddl, Reserved):
                result = self._manager().constraints.ensure(ddl.spec, replace_blocking_index=True)
                logger.debug(
                    "Schema DDL on '%s' named Drivine's own constraint on %s(%s), ensured as Drivine ensures it: %s",
                    self.database,
                    ddl.spec.label,
                    ", ".join(ddl.spec.properties),
                    result.status,
                )
            elif isinstance(ddl, Create):
                self._create(ddl)
            else:
                self._drop(ddl)

    def _recorded(self, gom: StatelessGraphObjectManager) -> list[_Recorded]:
        """Every index and constraint the store holds, with their aliases."""
        found = [
            _Recorded(kind, it)
            for kind in ("RANGE", "VECTOR", "FULLTEXT")
            for it in gom.indexes.list(kind)  # pyright: ignore[reportArgumentType]
        ]
        return [*found, *(_Recorded("UNIQUE", it) for it in gom.constraints.list())]

    def _create(self, ddl: Create) -> None:
        gom = self._manager()
        spec = ddl.spec
        if isinstance(spec, TextIndexSpec):
            raise unsupported_text_index(spec, gom.indexes.grammar.engine)
        name = ddl.alias or default_name(spec)
        items = self._recorded(gom)
        namesake = next(
            (it for it in items if (it.item.name == name or name in it.item.aliases) and not _same(it, spec)), None
        )
        if namesake is not None:
            raise DrivineError(
                f"There is already a schema item named '{name}' on '{self.database}': {_described(namesake)}. "
                f"{_described_spec(spec)} is not created under that name."
            )
        existing = next((it for it in items if _same(it, spec)), None)
        if existing is not None:
            equivalent = _equivalent(gom, existing, spec)
            if not ddl.if_not_exists:
                what = _what(spec)
                raise DrivineError(
                    f"An equivalent {what} already exists on '{self.database}': {_described(existing)}."
                    if equivalent
                    else f"A {what} on {spec.label}[{', '.join(spec.properties)}] already exists on "
                    f"'{self.database}', of another shape: {_described(existing)}."
                )
            if not equivalent:
                logger.warning(
                    "Schema DDL on '%s': %s exists already, of another shape than %s, and IF NOT EXISTS leaves it as "
                    "it is",
                    self.database,
                    _described(existing),
                    _described_spec(spec),
                )
                return
            # The item there, over the same properties in another order, is the one the spec is ensured as: the store
            # keeps one item of an identity, and the inventory names the one it holds.
            spec = replace(spec, properties=existing.item.properties)  # pyright: ignore[reportArgumentType]
        self.schema_manager.ensure_catalog(SchemaCatalog((spec,), name=OWNER).for_database(self.database))
        held = existing or next((it for it in self._recorded(gom) if _same(it, spec)), None)
        if held is None:
            return
        # The DDL's name, or the API's name for its spec, where the item goes by neither: as over an item of the same
        # properties in another order, whose name the API gives of that order.
        if name == held.item.name or name in held.item.aliases:
            return
        gom.indexes.alias(held.kind, held.item, (*held.item.aliases, name))
        logger.info("Schema DDL on '%s': %s goes by '%s' too", self.database, _described(held), name)

    def _drop(self, ddl: Drop) -> None:
        gom = self._manager()
        items = self._recorded(gom)
        what = ddl.kind.lower()
        found = next((it for it in items if it.item.name == ddl.name), None) or next(
            (it for it in items if ddl.name in it.item.aliases), None
        )
        spec: SchemaSpec | None = (
            spec_of(found)
            if found is not None
            else next(
                (it for it in self.schema_manager.ensured(OWNER) if default_name(it) == ddl.name),
                None,
            )
        )
        if spec is None:
            if ddl.if_exists:
                return
            raise DrivineError(f"There is no {what} named '{ddl.name}' on '{self.database}' to drop.")
        index = not isinstance(spec, UniquenessConstraintSpec)
        if index != (ddl.kind == "INDEX"):
            other, drop = ("an index", "INDEX") if index else ("a constraint", "CONSTRAINT")
            raise DrivineError(
                f"'{ddl.name}' is {other} on '{self.database}', {_described_spec(spec)}: DROP {ddl.kind} does not "
                f"drop it, and DROP {drop} does."
            )
        if schema_ddl.reserved(spec):
            raise DrivineError(
                f"'{ddl.name}' is Drivine's own constraint on {spec.label}[{', '.join(spec.properties)}], which "
                "Drivine keeps, and is not dropped."
            )
        self.schema_manager.remove_catalog(SchemaCatalog((spec,), name=OWNER).for_database(self.database))
        # An item another owner declares is left in place by the removal: it no longer goes by this DDL's name.
        kept = next((it for it in self._recorded(gom) if ddl.name in it.item.aliases), None)
        if kept is not None:
            gom.indexes.alias(kept.kind, kept.item, [it for it in kept.item.aliases if it != ddl.name])


def spec_of(recorded: _Recorded) -> SchemaSpec:
    """The spec of an item the store holds, by its kind, label, properties and name."""
    info = recorded.item
    if recorded.kind == "VECTOR":
        return VectorIndexSpec(info.label, (info.properties[0],), info.dimensions or 1, info.similarity or "COSINE")
    if recorded.kind == "FULLTEXT":
        return FullTextIndexSpec(info.label, info.properties)
    spec: RangeIndexSpec | UniquenessConstraintSpec = (
        RangeIndexSpec(info.label, info.properties)
        if recorded.kind == "RANGE"
        else UniquenessConstraintSpec(info.label, info.properties)
    )
    # Named where the store names it otherwise than the API would, as one made over the lowercased copies is.
    name = info.name.strip() if info.name else None
    return replace(spec, name=name) if name and name != default_name(spec) else spec


def unsupported_text_index(spec: TextIndexSpec, engine: str) -> DrivineError:
    """Why a TEXT index isn't made on the engine: it is Neo4j's. drivine4j's `unsupportedTextIndex`."""
    return DrivineError(
        f"TEXT index {default_name(spec)} on {spec.label}[{', '.join(spec.properties)}] cannot be made on {engine}: "
        f"a TEXT index is Neo4j's, and {engine} has none. For tokenized search declare a full-text index "
        "(FullTextIndex); for exact lookups, a range index."
    )


def _same(recorded: _Recorded, spec: DdlSpec) -> bool:
    """Whether the item is of the spec's identity: its kind, label, and properties in any order."""
    return (
        recorded.kind == spec.kind
        and recorded.item.label == spec.label
        and set(recorded.item.properties) == set(spec.properties)
    )


def _equivalent(gom: StatelessGraphObjectManager, recorded: _Recorded, spec: SchemaSpec) -> bool:
    """Whether the item, of the spec's identity, is of its shape too."""
    if isinstance(spec, (VectorIndexSpec, FullTextIndexSpec)):
        return gom.indexes.matches(spec, recorded.item)
    return True


def _what(spec: DdlSpec) -> str:
    return "constraint" if isinstance(spec, UniquenessConstraintSpec) else "index"


def _described(recorded: _Recorded) -> str:
    info = recorded.item
    return " ".join(
        it
        for it in (CATALOG_KINDS[recorded.kind], info.name or "", f"on {info.label}[{', '.join(info.properties)}]")
        if it
    )


def _described_spec(spec: DdlSpec) -> str:
    kind = "TEXT_INDEX" if isinstance(spec, TextIndexSpec) else CATALOG_KINDS[spec.kind]
    return f"{kind} {default_name(spec)} on {spec.label}[{', '.join(spec.properties)}]"
