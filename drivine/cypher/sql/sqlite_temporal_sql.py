from __future__ import annotations

from datetime import UTC, datetime

from drivine.backend.sqlite import sqlite_graph
from drivine.cypher.sql.temporal import Temporal
from drivine.cypher.sql.temporal_sql import TRUNCATION_UNITS, TemporalSql


class SqliteTemporalSql(TemporalSql):
    """
    SQLite's SQL for temporal values: see TemporalSql. SQLite has no type for a moment and no arithmetic of the
    calendar, so a timestamp is the text of a wall clock to the microsecond, `2024-03-15T10:30:00.000000`, which
    compares and orders as the clock does, and what is worked out of one is worked out by `drivine_temporal`, a function
    Drivine registers on each connection. drivine4j's SqliteTemporalSql, ported.
    """

    def __init__(self, shared: object) -> None:
        super().__init__(shared)  # type: ignore[arg-type]
        # The clock, in UTC, as the statement is compiled, just before it runs: the same for each of its rows and each
        # step of a write, as a statement's clock is in Cypher. SQLite's own `'now'` is the same within one step of a
        # query alone.
        self._now = f"'{datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%S.%f')}'"

    def local(self, text: str, kind: Temporal) -> str:
        return self._temporal("local", text, f"'{kind.name}'")

    def utc(self, text: str, kind: Temporal) -> str:
        return self._temporal("utc", text, f"'{kind.name}'")

    @property
    def now(self) -> str:
        return self._now

    def matches(self, text: str, pattern: str) -> str:
        return f"({text} REGEXP {pattern})"

    def group(self, text: str, pattern: str, group: int) -> str:
        return f"{sqlite_graph.REGEX_GROUP}({text}, {pattern}, {group})"

    def number(self, text: str) -> str:
        return f"CAST(NULLIF({text}, '') AS NUMERIC)"

    def sortable(self, timestamp: str) -> str:
        """A timestamp is such a text already."""
        return timestamp

    def micros(self, timestamp: str) -> str:
        """The last six digits, for a timestamp of a year BC is a character longer: `-0044-03-15T10:30:00.000000`."""
        return f"substr({timestamp}, -6)"

    def day_of_week(self, timestamp: str) -> str:
        """`drivine_temporal`'s, for SQLite's own `strftime` reads no year before 0."""
        return self._temporal("day_of_week", timestamp)

    def add_months(self, timestamp: str, months: str) -> str:
        return self._temporal("add_months", timestamp, months)

    def add_time(self, timestamp: str, days: str, seconds: str) -> str:
        return self._temporal("add_time", timestamp, days, seconds)

    def months_between(self, from_: str, to: str) -> str:
        return self._temporal("months_between", from_, to)

    def seconds_between(self, from_: str, to: str) -> str:
        return self._temporal("seconds_between", from_, to)

    def truncate(self, unit: str, timestamp: str) -> str | None:
        return self._temporal("truncate", timestamp, f"'{unit}'") if unit in TRUNCATION_UNITS else None

    def replace(self, text: str, pattern: str, replacement: str) -> str:
        return f"{sqlite_graph.REGEX_REPLACE}({text}, {pattern}, {replacement})"

    to_char = sqlite_graph.TO_CHAR

    trunc = sqlite_graph.TRUNC

    epoch = "'1970-01-01T00:00:00.000000'"

    def quotient(self, dividend: str, divisor: str) -> str:
        """SQLite divides one whole number by another to a whole number: the dividend is made a real number first."""
        return f"({dividend} + 0.0) / {divisor}"

    def extract(self, field: str, timestamp: str) -> str:
        return self._temporal("extract", timestamp, f"'{field}'")

    def _temporal(self, operation: str, *arguments: str) -> str:
        """`drivine_temporal`'s operation of the arguments."""
        return f"{sqlite_graph.TEMPORAL}('{operation}', {', '.join(arguments)})"
