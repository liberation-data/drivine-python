from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from drivine.drivine_error import DrivineError
from drivine.query.query_specification import QuerySpecification
from drivine.query.query_specification_compiler import QuerySpecificationCompiler


class Neo4jSpecCompiler(QuerySpecificationCompiler):
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
