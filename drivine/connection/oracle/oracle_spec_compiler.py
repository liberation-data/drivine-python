from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from drivine.backend.sql.sql_json import to_json
from drivine.drivine_error import DrivineError
from drivine.query.query_specification import QuerySpecification
from drivine.query.query_specification_compiler import QuerySpecificationCompiler

_PARAMETER = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


class OracleSpecCompiler(QuerySpecificationCompiler):
    """
    Compiles a specification for Oracle: `$name` parameters become `:name` binds, and skip and limit an
    `OFFSET … FETCH`. A list or a map is bound as JSON text, which a statement reads with `JSON(:name)` or
    `JSON_TABLE`.
    """

    def __init__(self, spec: QuerySpecification[Any]) -> None:
        super().__init__(spec)
        if self.statement.language != "SQL":
            raise DrivineError(f"{self.statement.language} is not supported on Oracle.", spec)
        if not isinstance(spec.parameters, Mapping) and spec.parameters:
            raise DrivineError("Oracle takes named parameters ($name), not a list of them.", spec)

    def formatted_statement(self) -> str:
        """
        Every `$name` the parameters hold becomes a bind, `:b0`, `:b1`, … (an Oracle bind name may not begin with `_`,
        as Drivine's own parameters do); a name is never matched as the prefix of a longer one.
        """
        names = list(self._named())

        def bound(match: re.Match[str]) -> str:
            name = match.group(1)
            return f":b{names.index(name)}" if name in names else match.group(0)

        return _PARAMETER.sub(bound, self.applied_statement())

    def formatted_parameters(self) -> dict[str, Any]:
        text = self.statement.text
        parameters: dict[str, Any] = {}
        for index, (name, value) in enumerate(self._named().items()):
            # Oracle refuses a bind the statement doesn't name.
            if re.search(rf"\${re.escape(name)}(?![A-Za-z0-9_])", text):
                parameters[f"b{index}"] = _bindable(value)
        return parameters

    def skip_clause(self) -> str:
        return f"OFFSET {self.spec._skip} ROWS" if self.spec._skip else ""

    def limit_clause(self) -> str:
        return f"FETCH NEXT {self.spec._limit} ROWS ONLY" if self.spec._limit else ""

    def _named(self) -> Mapping[str, Any]:
        """The parameters by name: a specification with none holds an empty list."""
        parameters = self.spec.parameters
        return parameters if isinstance(parameters, Mapping) else {}


def _bindable(value: Any) -> Any:
    if value is None or isinstance(value, (str, bytes, bool, int, float, datetime, date)):
        return value
    return to_json(value)
