from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

from drivine.query.specification import QuerySpecification


class GraphResultMapper(ABC):
    """
    Maps a driver's records to Python values, then applies the specification's post-processors. A record of one
    column is its value, and a record of several is a list of them.
    """

    def map_query_results(self, records: Sequence[Any], spec: QuerySpecification[Any]) -> list[Any]:
        return self.post_process(self.map_to_native(records), spec)

    def map_to_native(self, records: Sequence[Any]) -> list[Any]:
        results: list[Any] = []
        for record in records:
            values = self.values(record)
            if len(values) == 1:
                results.append(self.to_native(values[0]))
            else:
                results.append([self.to_native(it) for it in values])
        return results

    @staticmethod
    def post_process(results: list[Any], spec: QuerySpecification[Any]) -> list[Any]:
        for processor in spec.post_processors:
            results = processor.apply(results)
        return results

    @abstractmethod
    def values(self, record: Any) -> Sequence[Any]: ...

    @abstractmethod
    def to_native(self, value: Any) -> Any: ...
