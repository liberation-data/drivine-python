from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CompiledCypher:
    """
    A Cypher statement as SQL: the statement, and the values its parameters are bound to. Where the statement must be
    readied first, `prelude` is the statement that readies it, run before it, with no parameters: on Postgres, the
    function a statement that writes is compiled to, which psycopg sends apart from the statement that calls it, as a
    statement with parameters is one command.

    `calls_stored_nulls_equal` says whether the statement calls the function an Oracle graph is made with,
    `<graph>_nulls_equal`, which a store made by an earlier version, or by a user who may not make a function, does not
    have: see `OracleCypherCompiler.nulls_equal`.
    """

    sql: str
    parameters: dict[str, Any]
    prelude: str | None = None
    calls_stored_nulls_equal: bool = False
