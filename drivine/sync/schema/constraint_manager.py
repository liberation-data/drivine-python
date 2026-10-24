# Written by scripts/unasync.py from drivine/schema/constraint_manager.py:
# change that, and run the script again.
from __future__ import annotations

import logging
from collections.abc import Callable

from drivine.drivine_error import DrivineError
from drivine.schema.ensure_result import EnsureResult
from drivine.schema.schema_grammar import SchemaGrammar
from drivine.schema.schema_item import SchemaItem
from drivine.schema.schema_specs import range_index
from drivine.schema.uniqueness_constraint_spec import UniquenessConstraintSpec
from drivine.sync import concurrency
from drivine.sync.schema.index_manager import IndexManager
from drivine.sync.schema.schema_executor import SchemaExecutor

logger = logging.getLogger("drivine.schema")

VIOLATION_SAMPLE = 10
"""How many keys held more than once a constraint violation reports, and how many of the nodes that hold each."""


class ConstraintManager:
    """
    Uniqueness constraints, made idempotently. Before making one, the data is checked: if two nodes already hold the
    same values, nothing is made and the result is a VIOLATION with a sample of the keys, each naming the nodes that
    hold it. On Neo4j a plain range index over the same label and properties is in the way of one: the result is then
    BLOCKED_BY_INDEX, naming it, unless the index is to be replaced.
    """

    def __init__(
        self, executor: SchemaExecutor, grammar: SchemaGrammar, changed: Callable[[], None] = lambda: None
    ) -> None:
        """`changed` is told once this has made or dropped a constraint, as the object manager's list is then stale."""
        self.executor = executor
        self.grammar = grammar
        self.changed = changed

    def list(self) -> list[SchemaItem]:
        return self.grammar.list_uniques.read(self.executor.run(self.grammar.list_uniques.statement))

    def ensure(
        self, spec: UniquenessConstraintSpec, sample_size: int = VIOLATION_SAMPLE, replace_blocking_index: bool = False
    ) -> EnsureResult[UniquenessConstraintSpec]:
        """
        Makes the constraint unless it is there. The data is checked first, and up to `sample_size` keys held more than
        once are reported, each with up to `sample_size` of the nodes that hold it; 0 skips the check, leaving the
        engine to refuse a constraint the data breaks. FalkorDB builds the constraint in the background, and lists the
        build as failed: that raises DrivineError as soon as it is listed.

        A plain range index over the label and properties, in their order, that the engine says is in the way of the
        constraint (Neo4j) is reported as BLOCKED_BY_INDEX, and nothing is changed. With `replace_blocking_index`, the
        index is dropped and the constraint made, its own index taking the plain one's place. The data is read first,
        however `sample_size` is set: a key held more than once keeps the index and is a VIOLATION, with an empty sample
        when `sample_size` is 0, and data that can't be read keeps the index and raises the engine's refusal.
        """
        if self._exists(spec):
            return EnsureResult("ALREADY_MATCHING", spec)
        sample = self.executor.run(self.grammar.conflicts(spec, sample_size)) if sample_size > 0 else []
        if sample:
            return EnsureResult("VIOLATION", spec, sample=tuple(sample))
        if self.grammar.unique_needs_range:
            IndexManager(self.executor, self.grammar, self.changed).ensure(range_index(spec.label, spec.properties))
        try:
            for statement in self.grammar.create_unique(spec):
                self.executor.run(statement)
        except Exception as refusal:
            index = self._blocking_index(spec) if self.grammar.blocked_by_index(refusal) else None
            if index is None or not index.name:
                raise
            if not replace_blocking_index:
                logger.warning(
                    "Could not make the uniqueness constraint on %s(%s): the index %s is in the way of it",
                    spec.label,
                    ", ".join(spec.properties),
                    index.name,
                )
                return EnsureResult("BLOCKED_BY_INDEX", spec, index=index)
            if sample_size <= 0:
                # The engine refuses for the index before it reads the data, so a key held twice would refuse the
                # constraint once the index is gone: look first, and keep the index, as when the data can't be read.
                try:
                    repeated = self.executor.run(self.grammar.conflicts(spec, 1))
                except Exception as unreadable:
                    refusal.add_note(f"The index was kept: the data could not be read ({unreadable}).")
                    raise refusal from refusal.__cause__
                if repeated:
                    return EnsureResult("VIOLATION", spec, sample=())
            for statement in self.grammar.drop_named_range(index.name):
                self.executor.run(statement)
            self.changed()
            logger.info(
                "Dropped the index %s on %s(%s), in the way of a uniqueness constraint that replaces it",
                index.name,
                spec.label,
                ", ".join(spec.properties),
            )
            return self.ensure(spec, sample_size)
        self._await_built(spec)
        self.changed()
        return EnsureResult("CREATED", spec)

    def drop(self, spec: UniquenessConstraintSpec) -> None:
        if self._exists(spec):
            for statement in self.grammar.drop_unique(spec):
                self.executor.run(statement)
            self.changed()

    def _blocking_index(self, spec: UniquenessConstraintSpec) -> SchemaItem | None:
        """The plain range index over the constraint's label and properties, in its order; None when there is none."""
        held = IndexManager(self.executor, self.grammar).list("RANGE")
        return next((it for it in held if it.label == spec.label and it.properties == spec.properties), None)

    def _exists(self, spec: UniquenessConstraintSpec) -> bool:
        # Order means nothing to uniqueness, and engines reorder the properties.
        return any(it.label == spec.label and set(it.properties) == set(spec.properties) for it in self.list())

    def _failed(self, spec: UniquenessConstraintSpec) -> bool:
        """Whether the engine reports that its build of the constraint failed."""
        listing = self.grammar.list_failed_uniques
        if listing is None:
            return False
        held = listing.read(self.executor.run(listing.statement))
        return any(it.label == spec.label and set(it.properties) == set(spec.properties) for it in held)

    def _await_built(self, spec: UniquenessConstraintSpec) -> None:
        """FalkorDB builds a constraint in the background; wait until it reports it, or says it failed."""
        for attempt in range(50):
            if self._exists(spec):
                return
            if self._failed(spec):
                raise DrivineError(
                    f"{self.grammar.engine} could not build the uniqueness constraint on "
                    f"{spec.label}({', '.join(spec.properties)}): nodes may hold the same values."
                )
            concurrency.sleep(0.02 * (attempt + 1))
        raise DrivineError(f"The uniqueness constraint on {spec.label}({', '.join(spec.properties)}) was not built.")
