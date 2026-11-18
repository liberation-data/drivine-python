from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from drivine.drivine_error import DrivineError
from drivine.query.query_specification import QuerySpecification
from drivine.query.query_specification_compiler import QuerySpecificationCompiler
from drivine.query.sql_parameters import parameter_names, replace_parameters

_POSITIONAL = re.compile(r"\$(\d+)(?![0-9])")


class PostgresSpecCompiler(QuerySpecificationCompiler):
    """
    Compiles a specification for Postgres, through psycopg: each `$name` parameter the statement uses becomes psycopg's
    `%(name)s`, and a positional `$1` its `%(p1)s`. Only the names the statement uses outside its quoted text are
    bound: Postgres refuses a parameter its statement does not name, and a `$t` in a JSON path's `'$."at"."$t"'`, a
    dollar quote or a comment is none (see sql_parameters). With parameters, every other `%` is written `%%`, as
    psycopg reads a `%` as the start of a placeholder; a statement with none is left as it is, so a block's
    `format('%I …')` is untouched.
    """

    def __init__(self, spec: QuerySpecification[Any]) -> None:
        super().__init__(spec)
        if self.statement.language != "SQL":
            raise DrivineError(f"{self.statement.language} is not supported on Postgres.", spec)
        parameters = spec.parameters
        text = self.statement.text
        if isinstance(parameters, Mapping):
            # Only the names the statement uses outside its quoted text: Postgres refuses a parameter it does not name.
            used = parameter_names(text)
            self._names = sorted(key for key in parameters if key in used)
            self._values: dict[str, Any] = {key: parameters[key] for key in self._names}
        else:
            self._names = []
            self._values = {f"p{index + 1}": value for index, value in enumerate(parameters)}

    def formatted_statement(self) -> str:
        """
        Every `$name` bound outside quoted text becomes `%(name)s`, and every `$1` `%(p1)s`; a name is never matched
        as the prefix of a longer one ($id does not match $idList), nor inside quoted text: see sql_parameters.
        """
        statement = self.applied_statement()
        if not self._values:
            return statement
        escaped = statement.replace("%", "%%")
        if self._names:
            return replace_parameters(escaped, lambda name: f"%({name})s" if name in self._names else None)
        return _POSITIONAL.sub(
            lambda it: f"%(p{it.group(1)})s" if f"p{it.group(1)}" in self._values else it.group(0), escaped
        )

    def formatted_parameters(self) -> dict[str, Any]:
        return self._values
