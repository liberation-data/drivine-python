# Written by scripts/unasync.py from drivine/schema/index_manager.py:
# change that, and run the script again.
from __future__ import annotations

import dataclasses
import itertools
import logging
from collections.abc import Callable
from typing import Literal, TypeVar

from drivine.schema.ensure_result import EnsureResult
from drivine.schema.full_text_index_spec import FullTextIndexSpec
from drivine.schema.range_index_spec import RangeIndexSpec
from drivine.schema.schema_grammar import SchemaGrammar
from drivine.schema.schema_item import SchemaItem
from drivine.schema.schema_specs import IndexSpec, item_name
from drivine.schema.vector_index_spec import VectorIndexSpec
from drivine.sync.schema.schema_executor import SchemaExecutor

S = TypeVar("S", bound=RangeIndexSpec | VectorIndexSpec | FullTextIndexSpec)

logger = logging.getLogger("drivine.schema")


class IndexManager:
    """
    Range, vector and full-text indexes, made idempotently: `ensure` makes one only when the engine holds none over the
    label and properties, and reports DRIFT, changing nothing, when it holds one of another shape. `recreate` replaces.
    """

    def __init__(
        self, executor: SchemaExecutor, grammar: SchemaGrammar, changed: Callable[[], None] = lambda: None
    ) -> None:
        """`changed` is told once this has made or dropped an index: the object manager's index list is then stale."""
        self.executor = executor
        self.grammar = grammar
        self.changed = changed

    def list(self, kind: Literal["RANGE", "VECTOR", "FULLTEXT"] = "RANGE") -> list[SchemaItem]:
        """The indexes of a kind the engine holds."""
        listing = {
            "RANGE": self.grammar.list_ranges,
            "VECTOR": self.grammar.list_vectors,
            "FULLTEXT": self.grammar.list_full_texts,
        }[kind]
        return listing.read(self.executor.run(listing.statement))

    def ensure(self, spec: S) -> EnsureResult[S]:
        self._warn_unsupported(spec)
        found = self._find(spec)
        if found is not None:
            if self._matches(spec, found):
                return EnsureResult("ALREADY_MATCHING", spec)
            return EnsureResult("DRIFT", spec, found=found)
        self._create(spec)
        self.changed()
        self._log_effective(spec)
        return EnsureResult("CREATED", spec)

    def recreate(self, spec: S) -> EnsureResult[S]:
        """Drops the index over the label and properties, whatever its shape, and makes it as the spec says."""
        self.drop(spec)
        self._create(spec)
        self.changed()
        return EnsureResult("RECREATED", spec)

    def drop(self, spec: IndexSpec) -> None:
        found = self._find(spec)
        if found is None:
            return
        # Dropped as it was made: by the engine's name for it where it names items, else over the properties it holds.
        held: IndexSpec = dataclasses.replace(spec, properties=found.properties or spec.properties)  # pyright: ignore[reportArgumentType]
        if isinstance(held, FullTextIndexSpec):
            statements = self.grammar.drop_full_text(held, found.name)
        else:
            statements = self._statements(held, "drop")
        for statement in statements:
            self.executor.run(statement)
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

    def _log_effective(self, spec: IndexSpec) -> None:
        """Logs what the engine chose for a vector index it made, pinned or not, so the value can be told later."""
        if not isinstance(spec, VectorIndexSpec):
            return
        found = self._find(spec)
        if found is not None:
            shape = ", ".join(
                f"{key}={value}"
                for key, value in dataclasses.asdict(found).items()
                if value is not None and key not in ("label", "properties", "name")
            )
            logger.info("Vector index %s on %s[%s]: %s", item_name(spec), spec.label, spec.properties[0], shape)

    def _create(self, spec: IndexSpec) -> None:
        missing = self._missing(spec)
        if not missing.properties:
            return
        for statement in self._statements(missing, "create"):
            self.executor.run(statement)

    def _missing(self, spec: IndexSpec) -> IndexSpec:
        """
        The part of the spec the engine doesn't hold. On an engine that indexes properties one by one, a composite
        index's properties that are indexed already are left out: it refuses to index one twice.
        """
        if not isinstance(spec, RangeIndexSpec) or not self.grammar.ranges_per_property:
            return spec
        held = [prop for it in self.list("RANGE") if it.label == spec.label for prop in it.properties]
        return dataclasses.replace(spec, properties=tuple(it for it in spec.properties if it not in held))

    def _statements(self, spec: IndexSpec, action: Literal["create", "drop"]) -> list[str]:
        if isinstance(spec, VectorIndexSpec):
            return self.grammar.create_vector(spec) if action == "create" else self.grammar.drop_vector(spec)
        if isinstance(spec, FullTextIndexSpec):
            return (
                self.grammar.create_full_text(spec) if action == "create" else self.grammar.drop_full_text(spec, None)
            )
        return self.grammar.create_range(spec) if action == "create" else self.grammar.drop_range(spec)

    def _find(self, spec: IndexSpec) -> SchemaItem | None:
        """
        The index the engine holds for the spec: a vector index over the same property, the label's full-text index (one
        to a label), or a range index over the same properties.
        """
        items = [it for it in self.list(spec.kind) if it.label == spec.label]
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

    def _matches(self, spec: IndexSpec, found: SchemaItem) -> bool:
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
