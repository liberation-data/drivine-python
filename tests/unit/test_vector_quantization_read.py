"""Reading a Neo4j vector index's quantization: Neo4j up to 2026.05 reports `vector.quantization.enabled`, and from
2026.06 `vector.quantization.type`."""

from typing import Any

from drivine.schema.schema_grammar import NEO4J_SCHEMA


def quantization_read_from(**config: Any) -> bool | None:
    row = {"labels": ["P"], "properties": ["e"], "name": "v", "config": {"vector.dimensions": 8, **config}}
    return NEO4J_SCHEMA.list_vectors.read([row])[0].quantization_enabled


def test_reads_vector_quantization_enabled_which_neo4j_reports_up_to_2026_05():
    assert quantization_read_from(**{"vector.quantization.enabled": False}) is False
    assert quantization_read_from(**{"vector.quantization.enabled": True}) is True


def test_reads_vector_quantization_type_which_neo4j_reports_from_2026_06_as_enabled_unless_it_is_none():
    assert quantization_read_from(**{"vector.quantization.type": "NONE"}) is False
    assert quantization_read_from(**{"vector.quantization.type": "BINARY"}) is True
    assert quantization_read_from(**{"vector.quantization.type": "SCALAR"}) is True


def test_reads_vector_quantization_type_over_vector_quantization_enabled_when_both_are_reported():
    config = {"vector.quantization.type": "NONE", "vector.quantization.enabled": True}
    assert quantization_read_from(**config) is False


def test_reads_neither_as_none_not_as_disabled():
    assert quantization_read_from() is None
