"""Building schema specs, and the names Drivine gives them."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from typing import TypeAlias, TypeVar

from drivine.drivine_error import DrivineError
from drivine.model import lower_copies
from drivine.schema.engine_vector_options import EngineVectorOptions
from drivine.schema.full_text_index_spec import FullTextIndexSpec
from drivine.schema.range_index_spec import RangeIndexSpec
from drivine.schema.uniqueness_constraint_spec import UniquenessConstraintSpec
from drivine.schema.vector_index_spec import SimilarityFunction, VectorIndexSpec

IndexSpec: TypeAlias = RangeIndexSpec | VectorIndexSpec | FullTextIndexSpec
SchemaSpec: TypeAlias = IndexSpec | UniquenessConstraintSpec


def range_index(label: str, properties: str | Iterable[str], *, ignore_case: bool = False) -> RangeIndexSpec:
    """A range index; with `ignore_case`, over the lowercased copies of the properties (see RangeIndexSpec)."""
    return RangeIndexSpec(label, _names(properties), ignore_case=ignore_case)


def uniqueness_constraint(
    label: str, properties: str | Iterable[str], *, node_id_property: str | None = None, ignore_case: bool = False
) -> UniquenessConstraintSpec:
    """
    `node_id_property` names each node of a violation's sample, beside the engine's id; it changes nothing made. With
    `ignore_case`, the constraint is over the lowercased copies of the properties (see UniquenessConstraintSpec).
    """
    return UniquenessConstraintSpec(
        label, _names(properties), node_id_property=node_id_property, ignore_case=ignore_case
    )


def full_text_index(label: str, properties: str | Iterable[str]) -> FullTextIndexSpec:
    return FullTextIndexSpec(label, _names(properties))


def vector_index(
    label: str,
    property: str,
    dimensions: int,
    similarity: SimilarityFunction = "COSINE",
    *,
    hnsw_m: int | None = None,
    hnsw_ef_construction: int | None = None,
    engine_options: Iterable[EngineVectorOptions] = (),
) -> VectorIndexSpec:
    if isinstance(dimensions, bool) or not isinstance(dimensions, int) or dimensions < 1:  # pyright: ignore[reportUnnecessaryIsInstance]
        raise DrivineError(f"A vector index needs a whole number of dimensions, not {dimensions!r}.")
    _positive(hnsw_m, "hnsw_m")
    _positive(hnsw_ef_construction, "hnsw_ef_construction")
    options = tuple(engine_options)
    for it in options:
        _positive(it.hnsw_m, f"{it.engine} hnsw_m")
        _positive(it.hnsw_ef_construction, f"{it.engine} hnsw_ef_construction")
        _positive(it.ef_runtime, f"{it.engine} ef_runtime")
    return VectorIndexSpec(label, (property,), dimensions, similarity, None, hnsw_m, hnsw_ef_construction, options)


def item_name(spec: SchemaSpec) -> str:
    """
    The name Drivine gives an item on an engine that names them: drivine4j's default, `<label>_<properties>_<kind>`,
    so that a store written from the JVM, Node or Python holds one item, not several. One that ignores case is
    `<label>_<properties>_ci_<kind>`, its properties named as declared; a spec that names itself, as one made over the
    copies does (see `made_as`), goes by that name.
    """
    if isinstance(spec, (RangeIndexSpec, UniquenessConstraintSpec)) and spec.name:
        return spec.name
    suffix = {"RANGE": "range", "UNIQUE": "unique", "VECTOR": "vector", "FULLTEXT": "fulltext"}[spec.kind]
    ci = "ci_" if isinstance(spec, (RangeIndexSpec, UniquenessConstraintSpec)) and spec.ignore_case else ""
    return f"{spec.label}_{'_'.join(spec.properties)}_{ci}{suffix}"


_Spec = TypeVar("_Spec", bound=SchemaSpec)


def made_as(spec: _Spec) -> _Spec:
    """
    The item as it is made, drivine4j's `made()`: one that ignores case is made over the lowercased copies of its
    properties (see lower_copies), under its own name; any other as it is declared. What the store holds, its catalog
    records and an owner's inventory names is the item as it is made.
    """
    if not isinstance(spec, (RangeIndexSpec, UniquenessConstraintSpec)) or not spec.ignore_case:
        return spec
    made = dataclasses.replace(
        spec,
        properties=tuple(lower_copies.of(it) for it in spec.properties),
        name=item_name(spec),
        ignore_case=False,
    )
    return made  # pyright: ignore[reportReturnType]


def lowercased(spec: SchemaSpec) -> tuple[str, ...]:
    """The properties whose lowercased copies the item is made over: none for one that doesn't ignore case."""
    return spec.properties if isinstance(spec, (RangeIndexSpec, UniquenessConstraintSpec)) and spec.ignore_case else ()


def _names(properties: str | Iterable[str]) -> tuple[str, ...]:
    return (properties,) if isinstance(properties, str) else tuple(properties)


def _positive(value: int | None, what: str) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise DrivineError(f"{what} is a whole number of at least 1, not {value!r}.")
