from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from neo4j import Record
from neo4j.graph import Node, Path, Relationship
from neo4j.time import Date, DateTime, Time

from drivine.mapper.graph_result_mapper import GraphResultMapper


class Neo4jResultMapper(GraphResultMapper):
    """
    A node or relationship is its properties, and Neo4j's temporal values are the standard library's.
    """

    def values(self, record: Any) -> Sequence[Any]:
        return record.values()

    def to_native(self, value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, (Node, Relationship)):
            return {key: self.to_native(it) for key, it in value.items()}
        if isinstance(value, (DateTime, Date, Time)):
            return value.to_native()
        if isinstance(value, list):
            return [self.to_native(it) for it in value]  # pyright: ignore[reportUnknownVariableType]
        if isinstance(value, Record):
            return {key: self.to_native(value[key]) for key in value.keys()}
        if isinstance(value, dict):
            return {key: self.to_native(it) for key, it in value.items()}  # pyright: ignore[reportUnknownVariableType]
        if isinstance(value, Path):
            return [self.to_native(it) for it in value.nodes]
        return value
