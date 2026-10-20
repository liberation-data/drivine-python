"""Building schema specs, and the names Drivine gives them."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypeAlias

from drivine.drivine_error import DrivineError
from drivine.schema.engine_vector_options import EngineVectorOptions
from drivine.schema.full_text_index_spec import FullTextIndexSpec
from drivine.schema.range_index_spec import RangeIndexSpec
from drivine.schema.uniqueness_constraint_spec import UniquenessConstraintSpec
from drivine.schema.vector_index_spec import SimilarityFunction, VectorIndexSpec

IndexSpec: TypeAlias = RangeIndexSpec | VectorIndexSpec | FullTextIndexSpec
SchemaSpec: TypeAlias = IndexSpec | UniquenessConstraintSpec


def range_index(label: str, properties: str | Iterable[str]) -> RangeIndexSpec:
    return RangeIndexSpec(label, _names(properties))


def uniqueness_constraint(label: str, properties: str | Iterable[str]) -> UniquenessConstraintSpec:
    return UniquenessConstraintSpec(label, _names(properties))


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
    so that a store written from the JVM, Node or Python holds one item, not several.
    """
    suffix = {"RANGE": "range", "UNIQUE": "unique", "VECTOR": "vector", "FULLTEXT": "fulltext"}[spec.kind]
    return f"{spec.label}_{'_'.join(spec.properties)}_{suffix}"


def _names(properties: str | Iterable[str]) -> tuple[str, ...]:
    return (properties,) if isinstance(properties, str) else tuple(properties)


def _positive(value: int | None, what: str) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise DrivineError(f"{what} is a whole number of at least 1, not {value!r}.")
