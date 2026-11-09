from __future__ import annotations

import dataclasses
import itertools
import logging
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, Literal, NoReturn, TypeVar, cast

from drivine import concurrency
from drivine.drivine_error import DrivineError
from drivine.schema.ensure_result import EnsureResult
from drivine.schema.full_text_index_spec import FullTextIndexSpec
from drivine.schema.lower_copy_catalog import LowerCopyCatalog, fill_in_lower_copies
from drivine.schema.range_index_spec import RangeIndexSpec
from drivine.schema.schema_executor import SchemaExecutor
from drivine.schema.schema_grammar import SchemaGrammar
from drivine.schema.schema_item import SchemaItem
from drivine.schema.schema_specs import IndexSpec, item_name, made_as
from drivine.schema.vector_index_spec import VectorIndexSpec

if TYPE_CHECKING:
    from drivine.backend.sql.sql_catalog import CatalogKind

S = TypeVar("S", bound=RangeIndexSpec | VectorIndexSpec | FullTextIndexSpec)

logger = logging.getLogger("drivine.schema")

BUILD_TIMEOUT = 60.0
"""
How many seconds `ensure` and `recreate` wait, by default, for an index the engine builds in the background (Neo4j,
FalkorDB) to be built: as long as Drivine's own tests wait, with `db.awaitIndexes(60)`. `SchemaOptions` sets it.
"""

BUILD_POLL = 0.2
"""How many seconds apart FalkorDB is asked whether an index is built."""


class IndexNotBuilt(DrivineError):
    """
    An index `ensure` or `recreate` made, or found there, whose build failed (`failed`), or was still running when the
    wait ended. The index is left as it is. `result` is what `ensure` would have given had it been built.
    """

    def __init__(self, message: str, result: EnsureResult[Any], failed: bool) -> None:
        super().__init__(message)
        self.result = result
        self.failed = failed


class IndexManager:
    """
    Range, vector and full-text indexes, made idempotently: `ensure` makes one only when the engine holds none over the
    label and properties, and reports DRIFT, changing nothing, when it holds one of another shape. `recreate` replaces.
    """

    def __init__(
        self,
        executor: SchemaExecutor,
        grammar: SchemaGrammar,
        changed: Callable[[], None] = lambda: None,
        copies: LowerCopyCatalog | None = None,
        build_timeout: float = BUILD_TIMEOUT,
    ) -> None:
        """
        `changed` is told once this has made or dropped an index: the object manager's index list is then stale.
        `copies` fills in the lowercased copies an index that ignores case is made over; without it, such an index is
        refused. `build_timeout` is how many seconds an index the engine builds in the background is waited for.
        """
        self.executor = executor
        self.grammar = grammar
        self.changed = changed
        self.copies = copies
        self.build_timeout = build_timeout

    async def list(self, kind: Literal["RANGE", "VECTOR", "FULLTEXT"] = "RANGE") -> list[SchemaItem]:
        """The indexes of a kind the engine holds."""
        listing = {
            "RANGE": self.grammar.list_ranges,
            "VECTOR": self.grammar.list_vectors,
            "FULLTEXT": self.grammar.list_full_texts,
        }[kind]
        return listing.read(await self.executor.run(listing.statement))

    async def ensure(self, spec: S) -> EnsureResult[S]:
        """
        Makes the index unless the engine holds one over its label and properties. An index that ignores case (see
        RangeIndexSpec) is made over the lowercased copies, and first fills them in on the nodes of its label that have
        none, recording that they all have them, whatever the result. Where the index isn't there yet, the copies are
        filled in again even where recorded, as by `recreate`: those a field kept before it lost `CaseInsensitive` went
        out of step meanwhile. On an engine that builds an index in the background (Neo4j, FalkorDB), returns once the
        index, made here or there already, is built, and raises IndexNotBuilt for one whose build failed or did not end
        within `build_timeout` (see `_await_built`).
        """
        made = made_as(spec)
        self._warn_unsupported(made)
        found = await self._find(made)
        await fill_in_lower_copies(self.copies, spec, found is None)
        result = dataclasses.replace(await self._ensure_made(made, found), spec=spec)
        if result.status != "DRIFT":
            await self._await_built(made, result)
        return result

    async def _ensure_made(self, spec: S, found: SchemaItem | None) -> EnsureResult[S]:
        """`ensure`, of the index as it is made, which the engine holds as `found`."""
        if found is not None and found.incomplete:
            await self._create(spec)
            self.changed()
            logger.info(
                "Completed the %s index on %s(%s): it lacked part of what is made for it",
                spec.kind,
                spec.label,
                ", ".join(spec.properties),
            )
            return EnsureResult("CREATED", spec)
        if found is not None:
            if self.matches(spec, found):
                return EnsureResult("ALREADY_MATCHING", spec)
            return EnsureResult("DRIFT", spec, found=found)
        await self._create(spec)
        self.changed()
        await self._log_effective(spec)
        return EnsureResult("CREATED", spec)

    async def recreate(self, spec: S) -> EnsureResult[S]:
        """
        Drops the index over the label and properties, whatever its shape, and makes it as the spec says. An index that
        ignores case fills the lowercased copies in again on every node of its label, recorded or not, where Cypher or
        SQL of your own, which keeps no copy, left one out of step. The index made again keeps the aliases its record
        kept (see `alias`), and is returned once it is built, as by `ensure`.
        """
        await fill_in_lower_copies(self.copies, spec, True)
        made = made_as(spec)
        found = await self._find(made)
        await self.drop(spec)
        await self._create(made)
        if found is not None and found.aliases:
            await self.alias(made.kind, SchemaItem(made.label, made.properties), found.aliases)
        self.changed()
        result = EnsureResult("RECREATED", spec)
        await self._await_built(made, result)
        return result

    async def alias(self, kind: CatalogKind, item: SchemaItem, aliases: Sequence[str]) -> None:
        """
        Records the aliases as all those of the item, an index or a constraint of that kind whose record is there: the
        names Neo4j's schema DDL gave it where the schema API names it otherwise (see SchemaCommands). Nothing, on a
        store that keeps none.
        """
        if self.grammar.aliased is not None:
            await self.executor.run(self.grammar.aliased(kind, item, aliases))

    async def drop(self, declared: IndexSpec) -> None:
        spec = made_as(declared)
        found = await self._find(spec)
        if found is None:
            return
        # Dropped as it was made: by the engine's name for it where it names items, else over the properties it holds.
        held: IndexSpec = dataclasses.replace(spec, properties=found.properties or spec.properties)  # pyright: ignore[reportArgumentType]
        if isinstance(held, FullTextIndexSpec):
            statements = self.grammar.drop_full_text(held, found.name)
        else:
            statements = self._statements(held, "drop")
        for statement in statements:
            await self.executor.run(statement)
        self.changed()

    def _warn_unsupported(self, spec: IndexSpec) -> None:
        unsupported = self.grammar.unsupported_vector_tuning(spec) if isinstance(spec, VectorIndexSpec) else []
        if unsupported:
            logger.warning(
                "The vector index on %s(%s) pins %s, which %s can't express: they are not applied there.",
                spec.label,
                ", ".join(spec.properties),
                ", ".join(unsupported),
                self.grammar.engine,
            )

    async def _log_effective(self, spec: IndexSpec) -> None:
        """Logs what the engine chose for a vector index it made, pinned or not, so the value can be told later."""
        if not isinstance(spec, VectorIndexSpec):
            return
        found = await self._find(spec)
        if found is not None:
            shape = ", ".join(
                f"{key}={value}"
                for key, value in dataclasses.asdict(found).items()
                if value is not None and key not in ("label", "properties", "name", "aliases")
            )
            logger.info("Vector index %s on %s[%s]: %s", item_name(spec), spec.label, spec.properties[0], shape)

    async def _create(self, spec: IndexSpec) -> None:
        missing = await self._missing(spec)
        if not missing.properties:
            return
        for statement in self._statements(missing, "create"):
            await self.executor.run(statement)

    async def _await_built(self, spec: IndexSpec, result: EnsureResult[Any]) -> None:
        """
        Waits until the index of `result`, which `ensure` or `recreate` made or found there, as `spec` makes it, is
        built, on an engine that builds an index in the background, for at most `build_timeout` seconds. Neo4j returns
        from `CREATE INDEX` while the index is still POPULATING, and a write of a value too large for it that commits
        just as it comes online stops the database until it is restarted; FalkorDB answers a query from an index it is
        still building, with part of the rows. So nothing that makes an index hands it back before it is built.

        - Neo4j waits on the server, with `db.awaitIndex`, for the index by its name.
        - FalkorDB is polled, every BUILD_POLL seconds, until `db.indexes()` no longer lists the label's indexes as
          UNDER CONSTRUCTION (`list_index_statuses`).

        A build that failed, or did not end in time, raises IndexNotBuilt (see `_not_built`). The index is left as it
        is. Elsewhere the DDL returns the index built, and nothing is waited for.
        """
        if self.grammar.await_index is not None:
            found = await self._find(spec)
            if found is None or not found.name:
                # None listed: IF NOT EXISTS made none, as a uniqueness constraint's own index covers the properties,
                # which Neo4j builds within the statement that makes the constraint.
                return
            # By the name Neo4j holds it under: IF NOT EXISTS makes none over an index of the same shape another
            # process made under another name.
            name = found.name
            try:
                # Neo4j takes whole seconds.
                await self.executor.run(self.grammar.await_index, {"name": name, "timeout": int(self.build_timeout)})
            except Exception as error:
                # Neo4j's failure says why, and its state whether the build failed or was still running.
                state = await self._state(name)
                failed = state is not None and str(state.get("state")).upper() == "FAILED"
                reason = (state or {}).get("failure") if failed else None
                self._not_built(result, name, failed, str(reason or error), error)
            return
        listing = self.grammar.list_index_statuses
        if listing is None:
            return
        deadline = time.monotonic() + self.build_timeout

        async def status() -> SchemaItem | None:
            return next(
                (it for it in listing.read(await self.executor.run(listing.statement)) if it.label == spec.label), None
            )

        found = await status()
        while found is None or found.building:
            if time.monotonic() >= deadline:
                self._not_built(result, None, False, f"it is {found.status if found is not None else 'not listed'}")
            # Asked again at the deadline at the latest, so a wait shorter than the poll ends when it should.
            await concurrency.sleep(max(0.0, min(BUILD_POLL, deadline - time.monotonic())))
            found = await status()
        if found.failed:
            self._not_built(result, None, True, f"it is {found.status}")

    def _not_built(
        self,
        result: EnsureResult[Any],
        name: str | None,
        failed: bool,
        reason: str,
        cause: BaseException | None = None,
    ) -> NoReturn:
        """
        Raises IndexNotBuilt for the index of `result`: the one place a build that failed, or is still running once the
        wait is over, is raised. SchemaManager logs it instead under its WARN mode.
        """
        spec = result.spec
        named = f"{name} " if name else ""
        what = f"{self.grammar.engine} {spec.kind} index {named}on {spec.label}({', '.join(spec.properties)})"
        message = (
            f"{what} failed to build: {reason}"
            if failed
            else f"{what} is still being built, having waited at most {self.build_timeout:g} s: {reason}"
        )
        raise IndexNotBuilt(message, result, failed) from cause

    async def _state(self, name: str) -> dict[str, Any] | None:
        """The state Neo4j gives the index of that name, and its failure; None where there is none, or none is read."""
        if self.grammar.index_state is None:
            return None
        try:
            rows = await self.executor.run(self.grammar.index_state, {"name": name})
        except Exception as unreadable:
            logger.debug("Could not read the state of the index %s: %s", name, unreadable)
            return None
        row: Any = rows[0] if rows else None
        return cast("dict[str, Any]", row) if isinstance(row, dict) else None

    async def _missing(self, spec: IndexSpec) -> IndexSpec:
        """
        The part of the spec the engine doesn't hold. On an engine that indexes properties one by one, a composite
        index's properties that are indexed already are left out: it refuses to index one twice.
        """
        if not isinstance(spec, RangeIndexSpec) or not self.grammar.ranges_per_property:
            return spec
        held = [prop for it in await self.list("RANGE") if it.label == spec.label for prop in it.properties]
        return dataclasses.replace(spec, properties=tuple(it for it in spec.properties if it not in held))

    def _statements(self, spec: IndexSpec, action: Literal["create", "drop"]) -> list[str]:
        if isinstance(spec, VectorIndexSpec):
            return self.grammar.create_vector(spec) if action == "create" else self.grammar.drop_vector(spec)
        if isinstance(spec, FullTextIndexSpec):
            return (
                self.grammar.create_full_text(spec) if action == "create" else self.grammar.drop_full_text(spec, None)
            )
        return self.grammar.create_range(spec) if action == "create" else self.grammar.drop_range(spec)

    async def _find(self, spec: IndexSpec) -> SchemaItem | None:
        """
        The index the engine holds for the spec: a vector index over the same property, the label's full-text index (one
        to a label), or a range index over the same properties.
        """
        items = [it for it in await self.list(spec.kind) if it.label == spec.label]
        if isinstance(spec, VectorIndexSpec):
            return next((it for it in items if it.properties[:1] == spec.properties[:1]), None)
        if isinstance(spec, FullTextIndexSpec):
            # By its name where the engine names items (a label may hold several); FalkorDB holds one to a label.
            if not any(it.name for it in items):
                return items[0] if items else None
            # The same properties in another order name the same index; one made over the whole label reports none, so
            # its name is all there is to go by.
            return (
                next((it for it in items if it.name == item_name(spec)), None)
                or next((it for it in items if it.properties and set(it.properties) == set(spec.properties)), None)
                or next((it for it in items if it.name and it.name in _names_in_any_order(spec)), None)
            )
        if self.grammar.ranges_per_property:
            # An engine that indexes properties one by one holds a composite index when it holds each property's.
            every = all(any(it.properties[:1] == (prop,) for it in items) for prop in spec.properties)
            return SchemaItem(spec.label, spec.properties) if every else None
        return next((it for it in items if it.properties == spec.properties), None)

    def matches(self, spec: IndexSpec, found: SchemaItem) -> bool:
        """Whether the index the engine holds, `found` for the spec, is of the spec's shape: else it is drift."""
        if isinstance(spec, VectorIndexSpec):
            # Unpinned is never drift: with no declared value there is nothing for the engine to differ from. A pin the
            # engine reports otherwise is; one the engine doesn't report, or can't express, isn't checked.
            pinned = spec.tuning(self.grammar.engine)
            unsupported = self.grammar.unsupported_vector_tuning(spec)

            def agrees(key: str) -> bool:
                mine, theirs = getattr(pinned, key), getattr(found, key)
                return mine is None or theirs is None or key in unsupported or mine == theirs

            return (
                found.dimensions == spec.dimensions
                and found.similarity == spec.similarity
                and all(agrees(it) for it in ("hnsw_m", "hnsw_ef_construction", "quantization_enabled", "ef_runtime"))
            )
        if isinstance(spec, FullTextIndexSpec):
            # An index made over the whole label covers any properties of it.
            return set(found.properties) == set(spec.properties) or (
                self.grammar.full_text_over_label and len(spec.properties) > 1 and not found.properties
            )
        return True


def _names_in_any_order(spec: FullTextIndexSpec) -> set[str]:
    """The names a full-text index over the spec's properties has, whatever their order."""
    return {item_name(dataclasses.replace(spec, properties=it)) for it in itertools.permutations(spec.properties)}
