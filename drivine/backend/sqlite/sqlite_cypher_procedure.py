# apsw's stubs type a bytes value as collections.abc.Buffer, which Python 3.11 lacks: what they give is partly unknown.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
"""
`drivine_cypher(steps, parameters)`: carries out the procedure a Cypher statement that writes compiles to (see
`SqliteCypherCompiler.procedure`), as Postgres runs its PL/pgSQL and Oracle its PL/SQL, step by step on the connection
the statement runs on, in its transaction, and gives back the rows it returns as a JSON list. drivine4j's
`SqliteCypherFunction`.

`steps` is `{"steps": [...]}`, each step a map of its `op`, `select`, `loop`, `if`, `raise`, `execute`, `kept`, `keep`
or `return`;
its `sql`, in which each variable of the procedure it reads is a numbered parameter, `?1`; and `variables`, the names
bound to those parameters in turn: the parameters' object (`cy_params`), the rows carried from clause to clause, the
id a write made, a loop record's item (`cy_record.item`). A `select` sets its `variable` to the first column of the
first row its query gives; a `loop` sets its `record` to the first column of each row of its query, read whole before
its `body` writes anything, and runs the body for each; an `if` runs its `body` where its query gives 1; a `raise`
fails the procedure with its `message`; an `execute` runs its statement, and sets its `variable`, where it has one, to
what the statement gives; a `kept` sets its `variable` to an empty list of the rows a write keeps, and a `keep` adds
the first column of the first row its query gives to that list; a `return` adds the first column of each row its query
gives to the rows returned.

A variable holds the first column of a row as SQLite gives it, a whole number, a real one, or text (JSON's, where the
query gives JSON), and is bound again as it is: a number stays one, and a list a `kept` made is bound as the JSON list
of its rows. A variable not set is null. The steps run within a
savepoint of their own: a procedure that fails is rolled back to where it began, so the statement is applied whole or
not at all.
"""

from __future__ import annotations

import json
import math
from typing import Any, cast

import apsw

from drivine.backend.sqlite import sqlite_arguments as arguments
from drivine.drivine_error import DrivineError

PARAMS = "cy_params"
"""The variable the parameters' object is bound to: see `SqlCypherCompiler.PARAMS`."""

_SAVEPOINT = "drivine_cypher"


def run(connection: apsw.Connection, steps: Any, parameters: Any) -> str:
    """Carries out the procedure the steps make, with the parameters' object, and gives the JSON list it returns."""
    program = arguments.text(steps)
    if program is None:
        raise ValueError("drivine_cypher has no steps.")
    procedure = _Procedure(connection, arguments.text(parameters))
    connection.execute(f"SAVEPOINT {_SAVEPOINT}").close()
    try:
        procedure.run(cast(dict[str, Any], json.loads(program))["steps"])
        connection.execute(f"RELEASE {_SAVEPOINT}").close()
    except BaseException:
        for it in (f"ROLLBACK TO {_SAVEPOINT}", f"RELEASE {_SAVEPOINT}"):
            try:
                connection.execute(it).close()
            except apsw.Error:
                # The failure that stopped the procedure is the one to give.
                pass
        raise
    return "[" + ",".join(_row_text(it) for it in procedure.rows) + "]"


class _Kept(list[Any]):
    """
    The rows a write keeps, each the JSON text, or the number, a step gave: a step that reads them is given them as a
    JSON list (see `_Procedure._values`). Each `json_insert` of a row into the list as text read and wrote the whole
    list again (#419, liberation-data/drivine4j#588).
    """

    def json(self) -> str:
        return "[" + ",".join(_row_text(it) for it in self) + "]"


def _row_text(value: Any) -> str:
    """A row the procedure returns, as drivine4j writes it into the list: a real number as Java writes a double."""
    if value is None:
        return "null"
    if isinstance(value, float):
        return arguments.java_double(value)
    return arguments.text(value) or ""


def _java_int(value: int | float) -> int:
    """A number as Kotlin's `Number.toInt()`: a long's lowest 32 bits, a double cut toward zero within an int."""
    if isinstance(value, int):
        return arguments.int32(value)
    if math.isnan(value):
        return 0
    if value >= 2**31 - 1:
        return 2**31 - 1
    return -(2**31) if value <= -(2**31) else int(value)


class _Procedure:
    """A procedure as it is carried out: its variables, and the rows it has returned."""

    def __init__(self, connection: apsw.Connection, parameters: str | None) -> None:
        self.connection = connection
        self.variables: dict[str, Any] = {PARAMS: parameters}
        self.rows: list[Any] = []

    def run(self, steps: list[dict[str, Any]]) -> None:
        for step in steps:
            op = step.get("op")
            if op == "select":
                self.variables[str(step["variable"])] = self._first(step)
            elif op == "loop":
                for item in self._query(step):
                    self.variables[str(step["record"])] = item
                    self.run(step["body"])
            elif op == "if":
                found = self._first(step)
                if isinstance(found, (int, float)) and _java_int(found) == 1:
                    self.run(step["body"])
            elif op == "raise":
                raise DrivineError(str(step.get("message")))
            elif op == "execute":
                into = step.get("variable")
                if into is not None:
                    self.variables[str(into)] = self._first(step)
                else:
                    self.connection.execute(step["sql"], self._values(step)).close()
            elif op == "kept":
                self.variables[str(step["variable"])] = _Kept()
            elif op == "keep":
                self._kept(str(step["variable"])).append(self._first(step))
            elif op == "return":
                self.rows.extend(self._query(step))
            else:
                raise DrivineError(f"drivine_cypher has no step {op}.")

    def _query(self, step: dict[str, Any]) -> list[Any]:
        """The first column of each row the step's query gives, every row read before any step after it runs."""
        cursor = self.connection.execute(step["sql"], self._values(step))
        try:
            return [row[0] for row in cursor.fetchall()]
        finally:
            cursor.close()

    def _first(self, step: dict[str, Any]) -> Any:
        found = self._query(step)
        return found[0] if found else None

    def _kept(self, variable: str) -> _Kept:
        """The rows kept in the variable, a list a `kept` step made."""
        kept = self.variables.get(variable)
        if not isinstance(kept, _Kept):
            raise DrivineError(f"drivine_cypher keeps no rows in {variable}.")
        return kept

    def _values(self, step: dict[str, Any]) -> list[Any]:
        """
        The values of the variables the step reads, in the order its parameters are numbered: the rows a write keeps as
        a JSON list.
        """
        values = (self.variables.get(str(it)) for it in cast(list[Any], step.get("variables") or []))
        return [it.json() if isinstance(it, _Kept) else it for it in values]
