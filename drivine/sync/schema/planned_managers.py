# Written by scripts/unasync.py from drivine/schema/planned_managers.py:
# change that, and run the script again.
"""
The index and constraint managers of a SQL store (Oracle, Postgres, SQLite) in store layout 2: what each call makes,
drops and waits on is the shared module's schema plan (see schema_plan), so an item is made, found and dropped as
drivine4j and Drivine for TypeScript make, find and drop it, by the same statements. plans.yaml pins each call's
decisions. The graph engines keep drivine-python's own managers (IndexManager, ConstraintManager), as the compiler is
installed only for the SQL stores (liberation-data/drivine-python#515).

Their public calls are the graph engines' managers', so an application calls them alike on every store.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

from drivine.backend.postgres.postgres_statement import value_of
from drivine.backend.sql.sql_catalog import CatalogKind
from drivine.backend.store_statements import ModuleStore
from drivine.connection.database_type import DatabaseType
from drivine.drivine_error import DrivineError
from drivine.query.query_specification import QuerySpecification
from drivine.schema.ensure_result import EnsureResult
from drivine.schema.full_text_index_spec import FullTextIndexSpec
from drivine.schema.range_index_spec import RangeIndexSpec
from drivine.schema.schema_grammar import SchemaGrammar
from drivine.schema.schema_item import SchemaItem
from drivine.schema.schema_plan import (
    DEFAULT_BUILD_TIMEOUT,
    DEFAULT_VIOLATION_SAMPLE,
    KINDS,
    ItemPolicy,
    SchemaPlan,
    ensure_result,
    schema_item,
)
from drivine.schema.uniqueness_constraint_spec import UniquenessConstraintSpec
from drivine.schema.vector_index_spec import VectorIndexSpec
from drivine.sync import concurrency
from drivine.sync.schema.constraint_manager import ConstraintManager
from drivine.sync.schema.index_manager import IndexManager
from drivine.sync.schema.lower_copy_catalog import LowerCopyCatalog
from drivine.sync.schema.schema_executor import SchemaExecutor
from drivine.sync.schema.schema_plan_driver import listed, plan_item

SQL_STORES: dict[DatabaseType, ModuleStore] = {
    DatabaseType.ORACLE: "oracle",
    DatabaseType.POSTGRES: "postgres",
    DatabaseType.SQLITE: "sqlite",
}
"""The stores whose schema the shared module plans here, as it names them."""

IndexSpec = RangeIndexSpec | VectorIndexSpec | FullTextIndexSpec
S = TypeVar("S", RangeIndexSpec, VectorIndexSpec, FullTextIndexSpec)

BUILD_POLL = 0.1
"""How long, in seconds, `in_force` waits between looks at a constraint still being built."""


class SqlPlanHost:
    """
    How a SQL store's plan runs here: schema statements and listings on a connection of their own, outside any
    transaction (SchemaExecutor), the marker's through the object manager's persistence manager, Cypher where the plan
    says it is compiled, and the lowercased copies through the store's record of them.
    """

    def __init__(
        self,
        executor: SchemaExecutor,
        persistence_manager: Any,
        copies: LowerCopyCatalog | None,
        changed: Callable[[], None],
    ) -> None:
        self.executor = executor
        self.persistence_manager = persistence_manager
        self.copies = copies
        self._changed = changed

    def run(self, statement: str | Mapping[str, Any], parameters: Mapping[str, Any], once: bool = False) -> list[Any]:
        if not isinstance(statement, str):
            raise DrivineError(f"A SQL store runs no native command: {dict(statement)}.")
        return [value_of(it) for it in self.executor.run(statement, parameters, once=once)]

    def marker(self, statement: str, parameters: Mapping[str, Any], compiled: bool, rows: bool) -> list[Any]:
        from drivine.query.statement import cypher_statement

        spec = QuerySpecification[Any](cypher_statement(statement) if compiled else statement).bind(dict(parameters))
        if not rows:
            self.persistence_manager.execute(spec)
            return []
        return [value_of(it) for it in self.persistence_manager.query(spec)]

    def read_lower_copies(self) -> list[list[str]] | None:
        if self.copies is None:
            return None
        return [[label, property] for label, property in sorted(self.copies.stored())]

    def fill_lower_copies(self, label: str, properties: Sequence[str]) -> bool:
        if self.copies is None:
            return False
        self.copies.fill_in(label, properties, True)
        return True

    def changed(self) -> None:
        self._changed()


@dataclass(frozen=True)
class SchemaPlanning:
    """A SQL store's schema plan, the database it is for, and how its statements run here."""

    plan: SchemaPlan
    database: str
    host: SqlPlanHost


class PlannedIndexManager(IndexManager):
    """
    Range, vector and full-text indexes of a SQL store, made idempotently: `ensure` makes one only when the store holds
    none over the label and properties, and reports DRIFT, changing nothing, when it holds one of another shape.
    `recreate` replaces. An index that ignores case is made over the lowercased copies, filled in first. An
    IndexManager whose calls the module plans; what it reads of a shape (`matches`) is the store grammar's.
    """

    def __init__(
        self,
        planning: SchemaPlanning,
        grammar: SchemaGrammar,
        build_timeout: float = DEFAULT_BUILD_TIMEOUT / 1000,
        database: str | None = None,
    ) -> None:
        host = planning.host
        super().__init__(host.executor, grammar, host.changed, host.copies, build_timeout, database)
        self.planning = planning

    def list(self, kind: Literal["RANGE", "VECTOR", "FULLTEXT"] = "RANGE") -> list[SchemaItem]:
        """The indexes of a kind the store holds, each with the aliases its catalog record keeps."""
        name = KINDS[kind]
        statement = self.planning.plan.listings()["indexes"].get(name)
        found = listed(self.planning.plan, self.planning.host, "indexes", statement)
        return [schema_item(it) for it in found if it.get("kind") == name]

    def ensure(self, spec: S) -> EnsureResult[S]:
        """Makes the index unless the store holds one over its label and properties."""
        return self._noted(spec, self._result("ENSURE", spec))

    def recreate(self, spec: S) -> EnsureResult[S]:
        """
        Drops the index over the label and properties, whatever its shape, and makes it as the spec says. Destructive.
        Where something other than an index stands under the name Drivine gives the index (a table, say), it is left as
        it is and a DrivineError is raised, as drivine4j raises it.
        """
        result = self._result("RECREATE", spec)
        if result.status == "RECREATED":
            return self._noted(spec, result)
        index = f"{KINDS[spec.kind]} on {spec.label}[{', '.join(spec.properties)}]"
        if result.status == "DRIFT":
            name = result.found.name if result.found is not None else None
            raise DrivineError(
                f"Could not recreate {index}: {name}, an object Drivine didn't make, stands under its name. Drop or "
                "rename it: Drivine makes the item under that name, and recreate() leaves it."
            )
        raise DrivineError(f"Could not recreate {index}: {result.status}")

    def alias(self, kind: CatalogKind, item: SchemaItem, aliases: Sequence[str]) -> None:
        """Records `aliases` as all the aliases of the index or constraint of `kind` that is there."""
        statement = self.planning.plan.aliased(kind, item, aliases)
        if statement:
            self.planning.host.run(statement, {})

    def drop(self, declared: IndexSpec) -> None:  # pyright: ignore[reportIncompatibleMethodOverride]
        """Drops the index over the label and properties, where there is one."""
        plan_item(self.planning.plan, self.planning.database, self.planning.host, "DROP", declared)

    def _result(self, call: Literal["ENSURE", "RECREATE"], spec: S) -> EnsureResult[S]:
        policy = ItemPolicy(build_timeout_millis=int(self.build_timeout * 1000))
        item = plan_item(self.planning.plan, self.planning.database, self.planning.host, call, spec, policy)
        if item.get("result") is None:
            raise DrivineError(f"The plan of {call} of the {spec.kind} on {spec.label} gave no result.")
        return ensure_result(item["result"], spec)


class PlannedConstraintManager(ConstraintManager):
    """
    Uniqueness constraints of a SQL store, made idempotently. Where the data holds a key more than once, nothing is made
    and the result is a VIOLATION with a sample of the keys, each naming the nodes that hold it. A ConstraintManager
    whose calls the module plans.
    """

    def __init__(
        self,
        planning: SchemaPlanning,
        grammar: SchemaGrammar,
        build_timeout: float = DEFAULT_BUILD_TIMEOUT / 1000,
        database: str | None = None,
    ) -> None:
        host = planning.host
        super().__init__(host.executor, grammar, host.changed, host.copies, build_timeout, database)
        self.planning = planning

    def list(self) -> list[SchemaItem]:
        """The constraints the store holds and enforces, each with the aliases its catalog record keeps."""
        return [schema_item(it) for it in self._listed() if not it.get("buildFailed")]

    def ensure(
        self,
        spec: UniquenessConstraintSpec,
        sample_size: int = DEFAULT_VIOLATION_SAMPLE,
        replace_blocking_index: bool = False,
    ) -> EnsureResult[UniquenessConstraintSpec]:
        """Makes the constraint unless it is there: a key held more than once is a VIOLATION, up to `sample_size`."""
        return self._noted(spec, self._result("ENSURE", spec, sample_size, replace_blocking_index))

    def in_force(self, spec: UniquenessConstraintSpec) -> bool:
        """Whether the constraint is there and built, so that the store enforces it, as one another process made."""
        until = time.monotonic() + self.build_timeout
        while True:
            found = next(iter(self._listed(spec)), None)
            if found is None or found.get("buildFailed"):
                return False
            if not found.get("buildInProgress"):
                return True
            if time.monotonic() >= until:
                return False
            concurrency.sleep(BUILD_POLL)

    def recreate(
        self,
        spec: UniquenessConstraintSpec,
        sample_size: int = DEFAULT_VIOLATION_SAMPLE,
        replace_blocking_index: bool = False,
    ) -> EnsureResult[UniquenessConstraintSpec]:
        """
        Drops the constraint and makes it again, as `ensure` does. Its unique index is built anew beside the old one,
        which is dropped only once the new one is built, so a VIOLATION keeps the old one. The constraint made again
        keeps the aliases its record kept.
        """
        return self._noted(spec, self._result("RECREATE", spec, sample_size, replace_blocking_index))

    def drop(self, declared: UniquenessConstraintSpec) -> None:
        """Drops the constraint."""
        plan_item(self.planning.plan, self.planning.database, self.planning.host, "DROP", declared)

    def _listed(self, spec: UniquenessConstraintSpec | None = None) -> list[dict[str, Any]]:
        statement = self.planning.plan.listings()["constraints"]
        return listed(self.planning.plan, self.planning.host, "constraints", statement, spec)

    def _result(
        self,
        call: Literal["ENSURE", "RECREATE"],
        spec: UniquenessConstraintSpec,
        sample_size: int,
        replace_blocking_index: bool,
    ) -> EnsureResult[UniquenessConstraintSpec]:
        policy = ItemPolicy(
            violation_sample_size=sample_size,
            replace_blocking_index=replace_blocking_index,
            build_timeout_millis=int(self.build_timeout * 1000),
        )
        item = plan_item(self.planning.plan, self.planning.database, self.planning.host, call, spec, policy)
        if item.get("result") is None:
            raise DrivineError(f"The plan of {call} of the constraint on {spec.label} gave no result.")
        return ensure_result(item["result"], spec)
