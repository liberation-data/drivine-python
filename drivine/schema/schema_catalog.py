from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from drivine.drivine_error import DrivineError
from drivine.model.model_registry import ModelRegistry
from drivine.model.schema_declaration import SchemaDeclaration
from drivine.schema.range_index_spec import RangeIndexSpec
from drivine.schema.schema_specs import (
    SchemaSpec,
    full_text_index,
    made_as,
    range_index,
    uniqueness_constraint,
    vector_index,
)
from drivine.schema.uniqueness_constraint_spec import UniquenessConstraintSpec

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
    node_id_indexes: tuple[RangeIndexSpec, ...] = ()
    """
    The range indexes among `items` that fragments' NodeIds are given by default (`from_fragments`), each one no item
    declared serves, as drivine4j's catalog keeps them. `without_node_id_indexes` leaves them out.
    """

    @staticmethod
    def of(*items: SchemaSpec) -> SchemaCatalog:
        """The items, each once, indexes before constraints, as drivine4j's `SchemaCatalog.of` builds them."""
        return SchemaCatalog(_built(items))

    @staticmethod
    def from_fragments(
        *types: type,
        dimensions: VectorDimensionProvider | None = None,
        registry: ModelRegistry | None = None,
    ) -> SchemaCatalog:
        """
        The items fragments declare with RangeIndex, Unique, VectorIndex and FullTextIndex, over their first label and
        their fields' stored names, and a range index over each fragment's NodeId, as each save and each load by id
        finds its node by it, and without one reads every node of the label: unless the fragment opts out
        (`node_id_index=False`), or an item declared over the NodeId alone serves it (see `serves_node_id`), as
        drivine4j's `fromFragments` gives one (drivine4j#827). A vector index's dimensions come from `dimensions`
        unless its marker gives them. A uniqueness constraint names a violation's nodes by the fragment's NodeId, as it
        is stored. A range index or uniqueness constraint that ignores case is over the lowercased copies its
        `CaseInsensitive` fields keep.
        """
        models = registry or ModelRegistry()
        items: list[SchemaSpec] = []
        defaults: list[RangeIndexSpec] = []
        for type_ in types:
            model = models.fragment(type_)
            label = model.labels[0]
            default = _node_id_index(type_, label, model.node_id_property)
            if default is not None and default not in defaults:
                defaults.append(default)
            for it in _scanned(model.schema):
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
        return SchemaCatalog(_with_defaults(items, defaults), node_id_indexes=_kept(items, defaults))

    def without_node_id_indexes(self) -> SchemaCatalog:
        """
        The catalog without the range indexes fragments' NodeIds are given by default, leaving only what is declared,
        as drivine4j's `withoutNodeIdIndexes`, and SchemaOptions(index_node_ids=False), give it.
        """
        return dataclasses.replace(
            self, items=tuple(it for it in self.items if it not in self.node_id_indexes), node_id_indexes=()
        )

    @property
    def declared_items(self) -> tuple[SchemaSpec, ...]:
        """Its items but the NodeIds' range indexes it was given by default: what is declared."""
        return tuple(it for it in self.items if it not in self.node_id_indexes)

    def beside(self, declared: Sequence[SchemaSpec]) -> SchemaCatalog:
        """
        The catalog as it is applied beside `declared`, what the catalogs and the items ensured at runtime on a
        database declare: without a NodeId's default range index one of them serves, as drivine4j's `itemsBeside`
        gives it. One declared the same, unnamed, is the same item, which the default is kept beside, co-owned, so
        that this owner's marker doesn't call the index both share an orphan.
        """
        gone = {it for it in self.node_id_indexes if any(d != it and serves_node_id(d, it) for d in declared)}
        if not gone:
            return self
        return dataclasses.replace(
            self,
            items=tuple(it for it in self.items if it not in gone),
            node_id_indexes=tuple(it for it in self.node_id_indexes if it not in gone),
        )

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
        """The catalog with `more` among its items, built again; a NodeId's default range index one of them serves is
        left out."""
        declared = (*self.declared_items, *more)
        return dataclasses.replace(
            self,
            items=_with_defaults(declared, self.node_id_indexes),
            node_id_indexes=_kept(declared, self.node_id_indexes),
        )


def serves_node_id(declared: SchemaSpec, default: RangeIndexSpec) -> bool:
    """
    Whether `declared` serves `default`, a NodeId's range index over one property: a range index or a uniqueness
    constraint over that property alone, under the label, that doesn't ignore case, whose index a lookup by the id
    reads, as drivine4j's `servesNodeId` has it.
    """
    return (
        isinstance(declared, (RangeIndexSpec, UniquenessConstraintSpec))
        and declared.label == default.label
        and tuple(declared.properties) == tuple(default.properties)
        and not declared.ignore_case
    )


def _node_id_index(type_: type, label: str, node_id_property: str | None) -> RangeIndexSpec | None:
    """The range index the fragment's NodeId is given by default: none where it has no NodeId, or opts out."""
    if node_id_property is None or not getattr(type_, "__drivine_node_id_index__", True):
        return None
    return range_index(label, node_id_property)


_CLASS_KINDS = ("RANGE", "FULLTEXT", "UNIQUE")
"""The order drivine4j's scanner reads a fragment's class-level items in: by kind, each in the order declared."""


def _scanned(declared: Sequence[SchemaDeclaration]) -> list[SchemaDeclaration]:
    """
    A fragment's items in the order drivine4j's `FragmentSchemaScanner` gives them, which the planner is given them in
    (liberation-data/drivine-cypher-spec#135, its declarations.yaml): the class-level ones first, by kind, then the
    fields', by the field's name, as Kotlin's reflection lists a class's properties.
    """
    classes = [it for it in declared if not it.field]
    fields = [it for it in declared if it.field]
    return [it for kind in _CLASS_KINDS for it in classes if it.kind == kind] + sorted(fields, key=lambda it: it.field)


def _built(items: Sequence[SchemaSpec]) -> tuple[SchemaSpec, ...]:
    """
    The items as drivine4j's `SchemaCatalog.build` gives them: each once, by its kind, label and the properties it is
    made over, in the order first declared, and the indexes before the constraints. Of a uniqueness constraint declared
    with and without the NodeId its violations name nodes by, a fragment's and a hand-written one, the one that has it
    is kept. Two that differ otherwise are both kept, for the schema manager to refuse as a conflict.
    """
    kept: list[SchemaSpec] = []
    for item in items:
        same = next((i for i, it in enumerate(kept) if _key(it) == _key(item)), None)
        if same is None:
            kept.append(item)
            continue
        found = kept[same]
        if found == item:
            continue
        if isinstance(found, UniquenessConstraintSpec) and isinstance(item, UniquenessConstraintSpec):
            if dataclasses.replace(found, node_id_property=None) == dataclasses.replace(item, node_id_property=None):
                kept[same] = found if found.node_id_property is not None else item
                continue
        kept.append(item)
    return tuple(it for it in kept if it.kind != "UNIQUE") + tuple(it for it in kept if it.kind == "UNIQUE")


def _key(spec: SchemaSpec) -> tuple[str, str, frozenset[str]]:
    """What makes two declarations one item, as drivine4j keys them: kind, label and the properties it is made over."""
    made = made_as(spec)
    return (made.kind, made.label, frozenset(made.properties))


def _kept(declared: Sequence[SchemaSpec], defaults: Sequence[RangeIndexSpec]) -> tuple[RangeIndexSpec, ...]:
    """Each of the NodeIds' range indexes `defaults` once, that none of the items `declared` serves."""
    kept: list[RangeIndexSpec] = []
    for it in defaults:
        if it not in kept and not any(serves_node_id(item, it) for item in declared):
            kept.append(it)
    return tuple(kept)


def _with_defaults(declared: Sequence[SchemaSpec], defaults: Sequence[RangeIndexSpec]) -> tuple[SchemaSpec, ...]:
    """
    The items as drivine4j's `SchemaCatalog.build` gives them with the NodeIds' default range indexes: the declared
    items built (see `_built`), and the defaults none of them serves, once each, between the indexes and the
    constraints.
    """
    built = _built(declared)
    return (
        *(it for it in built if it.kind != "UNIQUE"),
        *_kept(declared, defaults),
        *(it for it in built if it.kind == "UNIQUE"),
    )
