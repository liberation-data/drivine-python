from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any, Literal, TypeVar

from drivine.cypher.shared_compiler import compile_cypher, drained
from drivine.cypher.sql.compiled_cypher import CompiledCypher
from drivine.drivine_error import DrivineError
from drivine.query.query_specification import QuerySpecification
from drivine.query.statement import Statement

T = TypeVar("T")

RESULT = "result"
"""The one column of the rows a compiled statement gives: see SqlCypherCompiler."""


def cypher_compiled(
    spec: QuerySpecification[T],
    store: Literal["POSTGRES", "ORACLE", "SQLITE"],
    graph: str,
    nulls_equal_stored: bool = True,
    era_stored: bool = True,
) -> tuple[QuerySpecification[T], CompiledCypher | None]:
    """
    The specification, with a Cypher statement compiled to the store's SQL over the graph's tables by the shared
    compiler (see shared_compiler), so a statement written for a graph engine runs here and gives the same rows;
    anything else as it is. Then the compiled statement, where it was compiled, whose `setup` readies it, each run first
    with no parameters (see CompiledCypher). On Oracle `nulls_equal_stored` and `era_stored` say whether it may call the
    functions the graph is made with, its stored `nulls_equal` and `era`: the compiler says which it calls.
    """
    statement = spec.statement
    if statement is None or statement.language != "CYPHER":
        return spec, None
    if not isinstance(spec.parameters, Mapping) and spec.parameters:
        raise DrivineError("A Cypher statement takes named parameters ($name), not a list of them.", spec)
    parameters: Mapping[str, Any] = spec.parameters if isinstance(spec.parameters, Mapping) else {}
    try:
        compiled = compile_cypher(statement.text, drained(parameters), store, graph, nulls_equal_stored, era_stored)
    except DrivineError as error:
        if error.spec is None:
            raise DrivineError(str(error), spec) from error
        raise
    sql = copy.copy(spec)
    sql.statement = Statement(compiled.sql, "SQL")
    sql.parameters = compiled.parameters
    return sql, compiled
