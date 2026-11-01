"""Which lowercased copies every node of a label has, and filling them in: drivine4j's `LowerCopyCatalog`."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from drivine.drivine_error import DrivineError
from drivine.schema.full_text_index_spec import FullTextIndexSpec
from drivine.schema.lower_copy_statements import LOWER_COPY_BATCH, LowerCopyStatements, column_of
from drivine.schema.range_index_spec import RangeIndexSpec
from drivine.schema.schema_specs import item_name, lowercased
from drivine.schema.uniqueness_constraint_spec import UniquenessConstraintSpec
from drivine.schema.vector_index_spec import VectorIndexSpec

logger = logging.getLogger("drivine.schema")

MAX_AGE = 60.0
"""How many seconds what another process recorded is taken as read, before it is read again."""


class LowerCopyCatalog:
    """
    Which `CaseInsensitive` properties have their lowercased copy (see lower_copies) on every node of a label, so that
    a query may compare with the copy: those an `ensure` of an index or a constraint that ignores case has filled in, as
    the store records them.

    What this manager has recorded is known at once. What another recorded is read from the store, on a schema
    connection of its own: the first time a property is asked after, and again once what was read is `max_age` seconds
    old. A property that isn't recorded counts as not filled in, and a query lowercases the property itself, which gives
    the same answer without the index. A record is never taken back: every save keeps the copy in step, and `recreate`,
    or an `ensure` that makes the item anew, fills it in again where a write that bypassed Drivine, or one made while
    the field was not `CaseInsensitive`, left it out of step.
    """

    def __init__(
        self,
        run: Callable[[str, Mapping[str, Any] | None], Awaitable[list[Any]]],
        statements: LowerCopyStatements,
        resolve_store: Callable[[], Awaitable[Any]],
        max_age: float = MAX_AGE,
        clock: Callable[[], float] = time.monotonic,
        batch: int = LOWER_COPY_BATCH,
    ) -> None:
        """
        `run` runs a statement on a schema connection of its own, outside any transaction; `resolve_store` makes the
        store's `DrivineStore` node where it isn't there, for the graph engines' record.
        """
        self._run = run
        self._statements = statements
        self._resolve_store = resolve_store
        self._max_age = max_age
        self._clock = clock
        self._batch = batch
        self._recorded: set[tuple[str, str]] = set()
        self._read: set[tuple[str, str]] | None = None
        self._read_at = 0.0

    async def filled_in(self, label: str, property: str) -> bool:
        """Whether every node of `label` is known to have the copy of `property`."""
        if (label, property) in self._recorded:
            return True
        if self._read is None or self._clock() - self._read_at >= self._max_age:
            await self._read_again()
        return self._read is not None and (label, property) in self._read

    async def filled(self, labels: Sequence[str], properties: Sequence[str]) -> set[str]:
        """The properties among `properties` whose copy every node of one of `labels` is known to have."""
        found: set[str] = set()
        for property in properties:
            for label in labels:
                if await self.filled_in(label, property):
                    found.add(property)
                    break
        return found

    async def fill_in(self, label: str, properties: Sequence[str], again: bool) -> None:
        """
        Fills in the copy of each of `properties` on the nodes of `label`, and records that they all have it; one
        recorded already is left as it is, unless `again`.
        """
        if not properties:
            return
        stored: set[tuple[str, str]] = set() if again else await self._stored()
        for property in [it for it in properties if again or (label, it) not in stored]:
            filled = 0
            while True:
                rows = await self._run(self._statements.fill(label, property, self._batch), None)
                written = int(column_of(rows[0])) if rows else 0
                filled += written
                if written < self._batch:
                    break
            # The graph engines keep the record on the store's own node, which is made where it isn't there.
            if self._statements.on_store_node:
                await self._resolve_store()
            recording = self._statements.record(label, property)
            await self._run(recording.statement, recording.bindings)
            self._recorded.add((label, property))
            logger.info(
                "Filled in the lowercased copy of %s.%s on %d node(s), and recorded that every node has it",
                label,
                property,
                filled,
            )

    async def _stored(self) -> set[tuple[str, str]]:
        """What the store records, read now."""
        return set(self._statements.entries(await self._run(self._statements.read, None)))

    async def _read_again(self) -> None:
        """Reads the record again. One that can't be read leaves what was read last."""
        at = self._clock()
        try:
            self._read = await self._stored()
        except Exception as e:
            # A store whose record can't be read is compared without the copies: slower, and the same answers.
            logger.debug("The record of lowercased copies could not be read, so they are not compared with: %s", e)
            self._read = self._read or set()
        self._read_at = at


async def fill_in_lower_copies(
    copies: LowerCopyCatalog | None,
    spec: RangeIndexSpec | UniquenessConstraintSpec | VectorIndexSpec | FullTextIndexSpec,
    again: bool,
) -> None:
    """
    Fills in the lowercased copies `spec` is made over, when it ignores case: see LowerCopyCatalog. With `again`, they
    are filled in whether or not they are recorded as there. Refused where there is no store to fill them in through.
    """
    properties = lowercased(spec)
    if not properties:
        return
    if copies is None:
        raise DrivineError(
            f"The {item_name(spec)} on {spec.label}({', '.join(spec.properties)}) ignores case, and this manager has "
            "no store to fill the lowercased copies in through."
        )
    await copies.fill_in(spec.label, properties, again)
