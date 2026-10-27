from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any, Literal, TypeVar

from drivine.cypher.sql.compiled_cypher import CompiledCypher
from drivine.drivine_error import DrivineError
from drivine.query.query_specification import QuerySpecification
from drivine.query.statement import Statement

T = TypeVar("T")

RESULT = "result"
"""The one column of the rows a compiled statement gives: see SqlCypherCompiler."""


def cypher_compiled(
    spec: QuerySpecification[T],
    store: Literal["POSTGRES", "ORACLE"],
    graph: str,
    nulls_equal_stored: bool = True,
) -> tuple[QuerySpecification[T], CompiledCypher | None]:
    """
    The specification, with a Cypher statement compiled to the store's SQL over the graph's tables (see
    SqlCypherCompiler), so a statement written for a graph engine runs here and gives the same rows; anything else as it
    is. Then the compiled statement, where it was compiled, whose `prelude` readies it, run first with no parameters
    (see CompiledCypher). On Oracle `nulls_equal_stored` says whether it may call the function the graph is made with:
    see `OracleCypherCompiler.nulls_equal`.
    """
    statement = spec.statement
    if statement is None or statement.language != "CYPHER":
        return spec, None
    if not isinstance(spec.parameters, Mapping) and spec.parameters:
        raise DrivineError("A Cypher statement takes named parameters ($name), not a list of them.", spec)
    parameters: Mapping[str, Any] = spec.parameters if isinstance(spec.parameters, Mapping) else {}
    try:
        if store == "POSTGRES":
            from drivine.cypher.sql.postgres_cypher_compiler import PostgresCypherCompiler

            compiled = PostgresCypherCompiler(graph).compile(statement.text, parameters)
        else:
            from drivine.cypher.sql.oracle_cypher_compiler import OracleCypherCompiler

            compiled = OracleCypherCompiler(graph, nulls_equal_stored).compile(statement.text, parameters)
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
    return sql, compiled
