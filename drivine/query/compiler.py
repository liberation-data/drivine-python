from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from drivine.errors import DrivineError
from drivine.query.specification import QuerySpecification


@dataclass(frozen=True)
class CompiledQuery:
    """
    A statement and its parameters, formatted for the target database.
    """

    statement: str
    parameters: dict[str, Any]


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


class CypherSpecCompiler(QuerySpecificationCompiler):
    """
    For the Bolt engines: positional parameters are named `1`, `2`, …, so `$1` in the statement reads the first.
    """

    def __init__(self, spec: QuerySpecification[Any]) -> None:
        super().__init__(spec)
        if self.statement.language != "CYPHER":
            raise DrivineError(f"{self.statement.language} is not supported on this database.", spec)

    def formatted_statement(self) -> str:
        return self.applied_statement()

    def formatted_parameters(self) -> dict[str, Any]:
        parameters = self.spec.parameters
        if isinstance(parameters, Mapping):
            return dict(parameters)
        return {str(index + 1): value for index, value in enumerate(parameters)}
