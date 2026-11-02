from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from drivine.drivine_error import DrivineError
from drivine.model.model_registry import ModelRegistry
from drivine.schema.schema_specs import SchemaSpec, full_text_index, range_index, uniqueness_constraint, vector_index

DEFAULT_SCOPE = "schema"
"""The owner of an unnamed catalog, as drivine4j names it."""

VectorDimensionProvider = Callable[[str, str], int]
"""The dimensions of the vectors stored under a label's property: those of the embedding model, known at run time."""


@dataclass(frozen=True)
class SchemaCatalog:
    """
    Indexes and constraints to hold on one or more databases, enforced by a SchemaManager. Immutable: each `with_` or
    `for_` gives a new catalog.
    """

    items: tuple[SchemaSpec, ...]
    version: str | None = None
    """A token whose change makes the next `enforce` recreate the catalog's items once."""
    databases: tuple[str, ...] | None = None
    """The databases it applies to; every graph database when None."""
    name: str = DEFAULT_SCOPE
    """
    Its owner: what its version and inventory are recorded under, in the owner's marker. Unnamed catalogs share the
    owner 'schema', drivine4j's default scope.
    """

    @staticmethod
    def of(*items: SchemaSpec) -> SchemaCatalog:
        return SchemaCatalog(tuple(items))

    @staticmethod
    def from_fragments(
        *types: type,
        dimensions: VectorDimensionProvider | None = None,
        registry: ModelRegistry | None = None,
    ) -> SchemaCatalog:
        """
        The items fragments declare with RangeIndex, Unique, VectorIndex and FullTextIndex, over their first label and
        their fields' stored names. A vector index's dimensions come from `dimensions` unless its marker gives them. A
        uniqueness constraint names a violation's nodes by the fragment's NodeId, as it is stored. A range index or
        uniqueness constraint that ignores case is over the lowercased copies its `CaseInsensitive` fields keep.
        """
        models = registry or ModelRegistry()
        items: list[SchemaSpec] = []
        for type_ in types:
            model = models.fragment(type_)
            label = model.labels[0]
            for it in model.schema:
                # An item that ignores case is over the lowercased copies a CaseInsensitive field keeps, and a save
                # keeps none of another field: it would hold the nodes there were when it was ensured, and none after.
                uncopied = [p for p in it.properties if p not in model.lower_copied] if it.ignore_case else []
                if uncopied:
                    raise DrivineError(
                        f"The {'uniqueness constraint' if it.kind == 'UNIQUE' else 'range index'} on "
                        f"{type_.__name__}({', '.join(it.properties)}) ignores case, and "
                        f"{', '.join(repr(p) for p in uncopied)} is not the property of a CaseInsensitive field, which "
                        "a save keeps a lowercased copy of. Add CaseInsensitive() to the field."
                    )
                if it.kind == "UNIQUE":
                    items.append(
                        uniqueness_constraint(
                            label, it.properties, node_id_property=model.node_id_property, ignore_case=it.ignore_case
                        )
                    )
                elif it.kind == "FULLTEXT":
                    items.append(full_text_index(label, it.properties))
                elif it.kind == "RANGE":
                    items.append(range_index(label, it.properties, ignore_case=it.ignore_case))
                else:
                    size = (
                        it.dimensions
                        if it.dimensions is not None
                        else (dimensions(label, it.properties[0]) if dimensions else None)
                    )
                    if size is None:
                        raise DrivineError(
                            f"The vector index on {type_.__name__}.{it.field} needs its dimensions: give them on "
                            f"VectorIndex, or a dimension provider to from_fragments."
                        )
                    items.append(
                        vector_index(
                            label,
                            it.properties[0],
                            size,
                            it.similarity,
                            hnsw_m=it.hnsw_m,
                            hnsw_ef_construction=it.hnsw_ef_construction,
                        )
                    )
        return SchemaCatalog(tuple(items))

    def with_version(self, version: str) -> SchemaCatalog:
        return dataclasses.replace(self, version=version)

    def named(self, name: str) -> SchemaCatalog:
        """Records its version and inventory under this owner, so that owners version apart."""
        return dataclasses.replace(self, name=name)

    def for_databases(self, *databases: str) -> SchemaCatalog:
        """Applies only to the databases named ('default' being the first registered). Replaces earlier targets."""
        return dataclasses.replace(self, databases=tuple(databases))

    def for_database(self, database: str) -> SchemaCatalog:
        return self.for_databases(database)

    def for_default_database(self) -> SchemaCatalog:
        return self.for_databases("default")

    def __add__(self, more: Sequence[SchemaSpec]) -> SchemaCatalog:
        return dataclasses.replace(self, items=(*self.items, *more))
