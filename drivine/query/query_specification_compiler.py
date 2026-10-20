from __future__ import annotations

from typing import Any

from drivine.drivine_error import DrivineError
from drivine.query.compiled_query import CompiledQuery
from drivine.query.query_specification import QuerySpecification


class QuerySpecificationCompiler:
    def __init__(self, spec: QuerySpecification[Any]) -> None:
        if spec.statement is None:
            raise DrivineError("A statement is required.", spec)
        self.spec = spec
        self.statement = spec.statement

    def compile(self) -> CompiledQuery:
        return CompiledQuery(self.formatted_statement().strip(), self.formatted_parameters())

    def applied_statement(self) -> str:
        return " ".join(it for it in (self.statement.text, self.skip_clause(), self.limit_clause()) if it)

    def skip_clause(self) -> str:
        if self.spec._skip:
            return f"{'SKIP' if self.statement.language == 'CYPHER' else 'OFFSET'} {self.spec._skip}"
        return ""

    def limit_clause(self) -> str:
        return f"LIMIT {self.spec._limit}" if self.spec._limit is not None else ""

    def formatted_statement(self) -> str:
        raise NotImplementedError

    def formatted_parameters(self) -> dict[str, Any]:
        raise NotImplementedError
