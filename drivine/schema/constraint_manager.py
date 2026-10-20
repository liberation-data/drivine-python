from __future__ import annotations

from drivine import concurrency
from drivine.drivine_error import DrivineError
from drivine.schema.ensure_result import EnsureResult
from drivine.schema.index_manager import IndexManager
from drivine.schema.schema_executor import SchemaExecutor
from drivine.schema.schema_grammar import SchemaGrammar
from drivine.schema.schema_item import SchemaItem
from drivine.schema.schema_specs import range_index
from drivine.schema.uniqueness_constraint_spec import UniquenessConstraintSpec

VIOLATION_SAMPLE = 10
"""How many conflicting values a constraint violation reports."""


class ConstraintManager:
    """
    Uniqueness constraints, made idempotently. Before making one, the data is checked: if two nodes already hold the
    same values, nothing is made and the result is a VIOLATION with a sample of the values.
    """

    def __init__(self, executor: SchemaExecutor, grammar: SchemaGrammar) -> None:
        self.executor = executor
        self.grammar = grammar

    async def list(self) -> list[SchemaItem]:
        return self.grammar.list_uniques.read(await self.executor.run(self.grammar.list_uniques.statement))

    async def ensure(
        self, spec: UniquenessConstraintSpec, sample_size: int = VIOLATION_SAMPLE
    ) -> EnsureResult[UniquenessConstraintSpec]:
        """
        Makes the constraint unless it is there. The data is checked first, and up to `sample_size` values held more
        than once are reported; 0 skips the check, leaving the engine to refuse a constraint the data breaks.
        """
        if await self._exists(spec):
            return EnsureResult("ALREADY_MATCHING", spec)
        sample = await self.executor.run(self.grammar.conflicts(spec, sample_size)) if sample_size > 0 else []
        if sample:
            return EnsureResult("VIOLATION", spec, sample=tuple(sample))
        if self.grammar.unique_needs_range:
            await IndexManager(self.executor, self.grammar).ensure(range_index(spec.label, spec.properties))
        for statement in self.grammar.create_unique(spec):
            await self.executor.run(statement)
        await self._await_built(spec)
        return EnsureResult("CREATED", spec)

    async def drop(self, spec: UniquenessConstraintSpec) -> None:
        if await self._exists(spec):
            for statement in self.grammar.drop_unique(spec):
                await self.executor.run(statement)

    async def _exists(self, spec: UniquenessConstraintSpec) -> bool:
        # Order means nothing to uniqueness, and engines reorder the properties.
        return any(it.label == spec.label and set(it.properties) == set(spec.properties) for it in await self.list())

    async def _await_built(self, spec: UniquenessConstraintSpec) -> None:
        """FalkorDB builds a constraint in the background; wait until it reports it, or says it failed."""
        for attempt in range(50):
            if await self._exists(spec):
                return
            await concurrency.sleep(0.02 * (attempt + 1))
        raise DrivineError(f"The uniqueness constraint on {spec.label}({', '.join(spec.properties)}) was not built.")
