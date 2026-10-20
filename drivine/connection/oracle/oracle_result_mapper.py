from __future__ import annotations

import json
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from drivine.mapper.graph_result_mapper import GraphResultMapper
from drivine.query.query_specification import QuerySpecification

_JSON_SUFFIX = ":json"


class OracleResultMapper:
    """
    Rows as Oracle gives them, keyed by column name, through the specification's post-processors. A row of one column
    is that column's value, as a record of one column is on the graph engines.

    A column named `<name>:json` holds JSON as text, which is parsed and named `<name>`: a PL/SQL block returns its
    JSON that way, as a returned result set with a JSON column gives only its prefetched rows in thin mode
    (oracle/node-oracledb#1795). A JSON number comes back as a Decimal, and is given as an int or a float.
    """

    def map_query_results(
        self, rows: Sequence[Sequence[Any]], columns: Sequence[str], spec: QuerySpecification[Any]
    ) -> list[Any]:
        return GraphResultMapper.post_process(self.map_rows(rows, columns), spec)

    def map_rows(self, rows: Sequence[Sequence[Any]], columns: Sequence[str]) -> list[Any]:
        results: list[Any] = []
        for row in rows:
            record: dict[str, Any] = {}
            for column, value in zip(columns, row, strict=True):
                if column.endswith(_JSON_SUFFIX):
                    record[column[: -len(_JSON_SUFFIX)]] = None if value is None else json.loads(value)
                else:
                    record[column] = _native(value)
            results.append(next(iter(record.values())) if len(record) == 1 else record)
        return results


def _native(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, list):
        return [_native(it) for it in value]  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, dict):
        return {key: _native(it) for key, it in value.items()}  # pyright: ignore[reportUnknownVariableType]
    return value
