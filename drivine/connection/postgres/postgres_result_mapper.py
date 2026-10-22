from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from drivine.mapper.graph_result_mapper import GraphResultMapper
from drivine.query.query_specification import QuerySpecification


class PostgresResultMapper:
    """
    Rows as Postgres gives them, each a dict of its columns, through the specification's post-processors. psycopg
    reads `jsonb` and `json` as Python values. A row of one column is left a dict, as plain SQL reads it: the object
    manager's statements read such a row as its value themselves (see PostgresStatement).
    """

    def map_query_results(self, rows: Sequence[dict[str, Any]], spec: QuerySpecification[Any]) -> list[Any]:
        return GraphResultMapper.post_process(list(rows), spec)
