from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any, Literal, TypeVar

from drivine.drivine_error import DrivineError
from drivine.query.query_specification import QuerySpecification
from drivine.query.statement import Statement

T = TypeVar("T")

RESULT = "result"
"""The one column of the rows a compiled statement gives: see SqlCypherCompiler."""


def cypher_compiled(
    spec: QuerySpecification[T], store: Literal["POSTGRES", "ORACLE"], graph: str
) -> tuple[QuerySpecification[T], str | None]:
    """
    The specification, with a Cypher statement compiled to the store's SQL over the graph's tables (see
    SqlCypherCompiler), so a statement written for a graph engine runs here and gives the same rows; anything else as it
    is. Then the statement that readies the compiled one, run first with no parameters, where it has one: see
    CompiledCypher.prelude.
    """
    statement = spec.statement
    if statement is None or statement.language != "CYPHER":
        return spec, None
    if not isinstance(spec.parameters, Mapping) and spec.parameters:
        raise DrivineError("A Cypher statement takes named parameters ($name), not a list of them.", spec)
    parameters: Mapping[str, Any] = spec.parameters if isinstance(spec.parameters, Mapping) else {}
    try:
        if store == "POSTGRES":
            from drivine.cypher.sql.postgres_cypher_compiler import PostgresCypherCompiler as Compiler
        else:
            from drivine.cypher.sql.oracle_cypher_compiler import OracleCypherCompiler as Compiler
        compiled = Compiler(graph).compile(statement.text, parameters)
    except ModuleNotFoundError as error:
        if error.name != "antlr4":
            raise
        extra = store.lower()
        raise DrivineError(
            f"A Cypher statement is compiled to SQL with ANTLR's runtime, which drivine[{extra}] installs: "
            f'pip install "drivine[{extra}]".',
            spec,
        ) from error
    sql = copy.copy(spec)
    sql.statement = Statement(compiled.sql, "SQL")
    sql.parameters = compiled.parameters
    return sql, compiled.prelude
