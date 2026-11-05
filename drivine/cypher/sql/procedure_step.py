"""
A step of the procedure a statement that writes compiles to: see SqlCypherCompiler. Its SQL names the procedure's
variables (ROWS, ROW …) as values it reads. A store with a procedural language writes the steps in it, as Postgres's
PL/pgSQL and Oracle's PL/SQL; one without carries them out one by one, as SQLite's `drivine_cypher` does. drivine4j's
ProcedureStep, ported.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Select:
    """`variable` set to the value of `expression` in the first row of `SELECT expression` and `from_`, or to null
    where it gives none."""

    variable: str
    expression: str
    from_: str


@dataclass(frozen=True)
class Let:
    """`variable` set to the value of `expression`, which reads no table."""

    variable: str
    expression: str


@dataclass(frozen=True)
class Loop:
    """
    `body` for each row of `query`, in its order, with the row's `item` column the value `record`.item: the rows are
    the query's as it starts, whatever the body writes. A query written over lines is `multiline`.
    """

    record: str
    query: str
    multiline: bool
    body: Sequence[ProcedureStep]


@dataclass(frozen=True)
class If:
    """`body`, where the SQL truth value `condition` holds."""

    condition: str
    body: Sequence[ProcedureStep]


@dataclass(frozen=True)
class Raise:
    """The procedure failed, with `message`: what it wrote is undone."""

    message: str


@dataclass(frozen=True)
class Execute:
    """The statement, which writes; with `into`, the value of the column it returns set to that variable."""

    statement: str
    into: str | None


@dataclass(frozen=True)
class Kept:
    """
    `list_` made an empty list of the rows a write keeps, in the store's own kind of list, which `Keep` adds a row to in
    a time that does not grow with the list: read as a JSON list at the end, by `SqlCypherCompiler.kept_rows`. A row
    appended to a JSON list copied the list, so a loop over many rows grew with their square (#419,
    liberation-data/drivine4j#588).
    """

    list_: str


@dataclass(frozen=True)
class Keep:
    """The JSON `item` kept after the rows of `list_`, a list `Kept` made."""

    list_: str
    item: str


@dataclass(frozen=True)
class Return:
    """The rows of `query`, whose one column is `result`, returned as the statement's rows."""

    query: str


ProcedureStep = Select | Let | Loop | If | Raise | Execute | Kept | Keep | Return
