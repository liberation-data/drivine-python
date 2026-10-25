# Written by scripts/unasync.py from drivine/schema/constraint_manager.py:
# change that, and run the script again.
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Literal

from drivine.drivine_error import DrivineError
from drivine.schema.ensure_result import EnsureResult
from drivine.schema.schema_grammar import SchemaGrammar
from drivine.schema.schema_item import SchemaItem
from drivine.schema.schema_listing import SchemaListing
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
        engine to refuse a constraint the data breaks. FalkorDB doesn't refuse one: it builds it in the background and
        lists the build as FAILED. The failed constraint is then dropped, and the result is a VIOLATION when the data
        shows a key held more than once, with up to `sample_size` of them (none at 0, though the data is still read to
        confirm it), as drivine4j gives. A build that fails with no key held more than once raises DrivineError. A
        constraint FalkorDB already lists as UNDER CONSTRUCTION, from another process or an earlier run, is waited on as
        one made here is: ALREADY_MATCHING once built, and dropped and made again once its build fails, as is one
        already listed as FAILED.

        A plain range index over the label and properties, in their order, that the engine says is in the way of the
        constraint (Neo4j) is reported as BLOCKED_BY_INDEX, and nothing is changed. With `replace_blocking_index`, the
        index is dropped and the constraint made, its own index taking the plain one's place. The data is read first,
        however `sample_size` is set: a key held more than once keeps the index and is a VIOLATION, with an empty sample
        when `sample_size` is 0, and data that can't be read keeps the index and raises the engine's refusal.
        """
        # One still being built, by another process or an earlier run, may yet fail: its build is waited on, as for one
        # made here. One dropped meanwhile is made again below.
        found = self._await_build(spec, until_listed=False)
        if found == "BUILT":
            return EnsureResult("ALREADY_MATCHING", spec)
        if found == "FAILED":
            # FalkorDB keeps listing a constraint whose build failed, though it enforces nothing. It isn't the
            # constraint: it is dropped and the constraint made again, which gives a VIOLATION when the data still holds
            # a key more than once.
            self._drop_failed_build(spec)
            self.changed()
            logger.warning(
                "Dropped the uniqueness constraint on %s(%s), whose build %s had listed as FAILED, to make it again",
                spec.label,
                ", ".join(spec.properties),
                self.grammar.engine,
            )
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
        return self._await_built(spec, sample_size)

    def drop(self, spec: UniquenessConstraintSpec) -> None:
        """Drops the constraint, or one whose build FalkorDB listed as FAILED, which `list` leaves out."""
        if self._exists(spec) or self._failed(spec):
            self._drop_unique(spec)
            self.changed()

    def _drop_unique(self, spec: UniquenessConstraintSpec) -> None:
        for statement in self.grammar.drop_unique(spec):
            self.executor.run(statement)

    def _drop_failed_build(self, spec: UniquenessConstraintSpec) -> None:
        """
        Drops the constraint, whose build FalkorDB listed as FAILED. Another process waiting on the same build sees it
        fail too, and may drop it first: FalkorDB then refuses the drop, as there is no such constraint, which is no
        error once the failed build is no longer listed. A refusal while it is still listed is raised.
        """
        try:
            self._drop_unique(spec)
        except Exception as refusal:
            if self._failed(spec):
                raise
            logger.debug(
                "The uniqueness constraint on %s(%s), whose build %s had listed as FAILED, was dropped already: %s",
                spec.label,
                ", ".join(spec.properties),
                self.grammar.engine,
                refusal,
            )

    def _blocking_index(self, spec: UniquenessConstraintSpec) -> SchemaItem | None:
        """The plain range index over the constraint's label and properties, in its order; None when there is none."""
        held = IndexManager(self.executor, self.grammar).list("RANGE")
        return next((it for it in held if it.label == spec.label and it.properties == spec.properties), None)

    def _exists(self, spec: UniquenessConstraintSpec) -> bool:
        # Order means nothing to uniqueness, and engines reorder the properties.
        return any(it.label == spec.label and set(it.properties) == set(spec.properties) for it in self.list())

    def _failed(self, spec: UniquenessConstraintSpec) -> bool:
        """Whether the engine reports that its build of the constraint failed."""
        return self._listed_by(self.grammar.list_failed_uniques, spec)

    def _listed_by(
        self, listing: SchemaListing | None, spec: UniquenessConstraintSpec, rows: dict[str, list[Any]] | None = None
    ) -> bool:
        """Whether `listing` lists the constraint; a statement already in `rows` is read from there, not run again."""
        if listing is None:
            return False
        if rows is None:
            rows = {}
        if listing.statement not in rows:
            rows[listing.statement] = self.executor.run(listing.statement)
        held = listing.read(rows[listing.statement])
        return any(it.label == spec.label and set(it.properties) == set(spec.properties) for it in held)

    def _await_built(self, spec: UniquenessConstraintSpec, sample_size: int) -> EnsureResult[UniquenessConstraintSpec]:
        """
        CREATED once the engine reports the constraint built, which FalkorDB does in the background. A build FalkorDB
        lists as FAILED gives what `_build_failed` gives.
        """
        if self._await_build(spec, until_listed=True) == "FAILED":
            return self._build_failed(spec, sample_size)
        self.changed()
        return EnsureResult("CREATED", spec)

    def _await_build(self, spec: UniquenessConstraintSpec, until_listed: bool) -> Literal["BUILT", "FAILED"] | None:
        """
        Reads the constraint until its build has ended: BUILT, or FAILED where the engine builds it in the background
        and the build failed. FalkorDB lists the constraint UNDER CONSTRUCTION as it builds it, and may yet list it as
        FAILED. While it isn't listed, reading goes on when `until_listed` (one just made, not yet listed), and
        otherwise ends with None (none there, or one dropped meanwhile).
        """
        for attempt in range(50):
            # One reading per attempt, so that a build marked FAILED between two readings isn't taken for built.
            rows: dict[str, list[Any]] = {}
            if self._listed_by(self.grammar.list_failed_uniques, spec, rows):
                return "FAILED"
            if self._listed_by(self.grammar.list_uniques, spec, rows):
                if not self._listed_by(self.grammar.list_building_uniques, spec, rows):
                    return "BUILT"
            elif not until_listed:
                return None
            concurrency.sleep(0.02 * (attempt + 1))
        raise DrivineError(f"The uniqueness constraint on {spec.label}({', '.join(spec.properties)}) was not built.")

    def _build_failed(self, spec: UniquenessConstraintSpec, sample_size: int) -> EnsureResult[UniquenessConstraintSpec]:
        """
        A constraint whose build FalkorDB listed as FAILED is dropped: it enforces nothing, yet stays listed, and
        drivine4j and TypeScript's Drivine, reading the same store, would take it for the constraint. It is then a
        VIOLATION, as in drivine4j, when a key is held more than once, with up to `sample_size` of them (none at 0,
        though the data is read to confirm it). A build that fails with no key held more than once, or data that can't
        be read, raises DrivineError.
        """
        self._drop_failed_build(spec)
        failed = (
            f"{self.grammar.engine} could not build the uniqueness constraint on "
            f"{spec.label}({', '.join(spec.properties)})"
        )
        try:
            sample = self.executor.run(self.grammar.conflicts(spec, max(sample_size, 1)))
        except Exception as unreadable:
            raise DrivineError(f"{failed}, and the data could not be read to see why.") from unreadable
        if not sample:
            raise DrivineError(f"{failed}, but no key it covers is held more than once.")
        return EnsureResult("VIOLATION", spec, sample=tuple(sample[: max(sample_size, 0)]))
