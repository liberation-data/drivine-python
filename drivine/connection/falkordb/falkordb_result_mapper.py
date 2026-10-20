from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from falkordb import Edge, Node, Path

from drivine.mapper.graph_result_mapper import GraphResultMapper


class FalkorDbResultMapper(GraphResultMapper):
    """
    Maps rows from the falkordb client, which are lists of column values. A node or relationship is its properties,
    as on Neo4j.
    """

    def values(self, record: Any) -> Sequence[Any]:
        return record

    def to_native(self, value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, (Node, Edge)):
            return {key: self.to_native(it) for key, it in value.properties.items()}  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if isinstance(value, Path):
            return [self.to_native(it) for it in value.nodes()]  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if isinstance(value, list):
            return [self.to_native(it) for it in value]  # pyright: ignore[reportUnknownVariableType]
        if isinstance(value, dict):
            return {key: self.to_native(it) for key, it in value.items()}  # pyright: ignore[reportUnknownVariableType]
        return value
