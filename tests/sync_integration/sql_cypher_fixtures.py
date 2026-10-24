# Written by scripts/unasync.py from tests/integration/sql_cypher_fixtures.py:
# change that, and run the script again.
"""
Lets a test written for the graph engines run on a SQL store, Postgres or Oracle, with its statements as they are.
drivine4j's `SqlCypherFixtures` does the same.

Such a test seeds its graph and reads it back in Cypher, through a `PersistenceManager`, with statements of no language:
a SQL store takes those for SQL. `over` gives a manager that sends such a statement that is Cypher as Cypher, which the
store compiles to its SQL, as it compiles any Cypher statement; any other statement goes as it is. A statement is told
for Cypher by what it opens with: a pattern (`MATCH`, `OPTIONAL MATCH`, `CREATE`, `MERGE`), named as a path or not,
`UNWIND`, `RETURN`, or a `//` comment, which SQL has none of. One that opens with `WITH` is taken for a common table
expression, and is SQL.
"""

from __future__ import annotations

import re
from typing import Any, TypeVar

from drivine.sync import CursorSpecification, DatabaseType, PersistenceManager, QuerySpecification
from drivine.sync.cursor.cursor import Cursor

T = TypeVar("T")
S = TypeVar("S", bound=QuerySpecification[Any])

SQL_TYPES = (DatabaseType.POSTGRES, DatabaseType.ORACLE)
"""The stores that keep a graph in SQL tables, and compile a Cypher statement to their SQL."""

CYPHER = re.compile(r"^\s*(?://|(?:OPTIONAL\s+MATCH|MATCH|CREATE|MERGE)\s*(?:\w+\s*=\s*)?\(|UNWIND\s|RETURN\s)", re.I)
"""
A statement that opens with a pattern, named as a path (`MATCH p = (…)`) or not, `UNWIND`, `RETURN`, or a `//` comment:
`CREATE INDEX` is the store's own, and so is one that opens with `WITH`, which SQL names a common table expression by.
"""


def as_cypher(spec: S) -> S:
    """The spec with its statement sent as Cypher, where it has no language and is Cypher."""
    statement = spec.statement
    if statement is not None and statement.language == "PLATFORM_DEFAULT" and CYPHER.match(statement.text):
        return spec.finalized_copy("CYPHER")
    return spec


class SqlCypherFixtures(PersistenceManager):
    """The manager given, with a statement of no language that is Cypher sent as Cypher, and every other as it is."""

    def __init__(self, manager: PersistenceManager) -> None:
        self.manager = manager
        self.database = manager.database
        self.type = manager.type
        # An object manager reads the graph's name from the provider.
        self.provider = getattr(manager, "provider", None)

    def query(self, spec: QuerySpecification[T]) -> list[T]:
        return self.manager.query(as_cypher(spec))

    def open_cursor(self, spec: CursorSpecification[T]) -> Cursor[T]:
        return self.manager.open_cursor(as_cypher(spec))


def for_store(manager: PersistenceManager) -> PersistenceManager:
    """The manager as a test that seeds and reads back in Cypher wants it: wrapped on a SQL store, itself on a graph."""
    return SqlCypherFixtures(manager) if manager.type in SQL_TYPES else manager
