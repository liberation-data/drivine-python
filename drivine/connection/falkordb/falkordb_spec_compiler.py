from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from drivine.connection.falkordb.falkordb_parameter_coercer import FalkorDbParameterCoercer
from drivine.drivine_error import DrivineError
from drivine.query.query_specification import QuerySpecification
from drivine.query.query_specification_compiler import QuerySpecificationCompiler

_POSITIONAL = re.compile(r"\$(\d+)\b")


class FalkorDbSpecCompiler(QuerySpecificationCompiler):
    """
    Positional parameters ($1, $2) are bound by name, as on Neo4j. FalkorDB names mustn't start with a digit, so they
    are rewritten to $p1, $p2 in the statement.
    """

    def __init__(self, spec: QuerySpecification[Any]) -> None:
        super().__init__(spec)
        if self.statement.language != "CYPHER":
            raise DrivineError(f"{self.statement.language} is not supported on FalkorDB.", spec)
        self._coercer = FalkorDbParameterCoercer()

    def formatted_statement(self) -> str:
        statement = self.applied_statement()
        if isinstance(self.spec.parameters, Mapping):
            return statement
        return _POSITIONAL.sub(r"$p\1", statement)

    def formatted_parameters(self) -> dict[str, Any]:
        parameters = self.spec.parameters
        if isinstance(parameters, Mapping):
            return self._coercer.coerce(parameters)
        return self._coercer.coerce({f"p{index + 1}": value for index, value in enumerate(parameters)})
