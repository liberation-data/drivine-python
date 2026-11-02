from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

from drivine.cypher.sql.temporal import OFFSET, Temporal

Shared = Callable[[list[str], Callable[[list[str]], str]], str]
"""The compiler's `shared`: a value it is given is written once where it is read more than once."""


TRUNCATION_UNITS = (
    "millennium",
    "century",
    "decade",
    "year",
    "weekYear",
    "quarter",
    "month",
    "week",
    "day",
    "hour",
    "minute",
    "second",
    "millisecond",
    "microsecond",
)
"""
The units a timestamp truncates to, as Cypher names them, from the longest. The week-based year starts on the Monday of
its first ISO week. A timestamp keeps microseconds alone, so cut to them it is the same.
"""

_YEARS_OF = {"millennium": 1000, "century": 100, "decade": 10}
"""
The units of many years, and how many: as Neo4j counts them, the year less its years into the span, counted toward zero
as Java divides. So 2017's millennium starts in 2000, 1984's century in 1900, and -1234's millennium in -1000.
"""


def years_of(unit: str) -> int | None:
    """How many years the unit is, where it is a unit of many: see _YEARS_OF."""
    return _YEARS_OF.get(unit)


class TemporalSql(ABC):
    """
    A store's SQL for temporal values, which Temporals works openCypher's out of. Its texts and timestamps are SQL: a
    text is a value's ISO 8601 text, a timestamp a SQL timestamp of no zone, a number a SQL number. `shared` is the
    compiler's: a value it is given is written once where it is read more than once.
    """

    def __init__(self, shared: Shared) -> None:
        self.shared = shared

    @abstractmethod
    def local(self, text: str, kind: Temporal) -> str:
        """The wall clock the text of the kind reads, an offset it has left out: a date at its midnight, a time of day
        on 1970-01-01."""

    @abstractmethod
    def utc(self, text: str, kind: Temporal) -> str:
        """The moment the text of the kind is, in UTC; for a kind of no zone, the wall clock it reads."""

    @property
    @abstractmethod
    def now(self) -> str:
        """The moment the statement runs, in UTC."""

    @abstractmethod
    def matches(self, text: str, pattern: str) -> str:
        """Whether the text matches the regular expression, a SQL text."""

    @abstractmethod
    def group(self, text: str, pattern: str, group: int) -> str:
        """The text the group of the regular expression matches in the text: null where it matches none."""

    @abstractmethod
    def number(self, text: str) -> str:
        """The text as a number: null where it is null or empty."""

    @abstractmethod
    def sortable(self, timestamp: str) -> str:
        """The timestamp as a text of one width, `2024-03-15T10:30:00.000000`, which orders as the timestamps do."""

    @abstractmethod
    def micros(self, timestamp: str) -> str:
        """The microseconds of the second of the timestamp, as six digits."""

    @abstractmethod
    def day_of_week(self, timestamp: str) -> str:
        """The ISO day of the week of the timestamp, from 1 for Monday to 7."""

    @abstractmethod
    def add_months(self, timestamp: str, months: str) -> str:
        """The timestamp so many months later, a day past the month's end its last day."""

    @abstractmethod
    def add_time(self, timestamp: str, days: str, seconds: str) -> str:
        """The timestamp so many days and seconds later, numbers each."""

    @abstractmethod
    def months_between(self, from_: str, to: str) -> str:
        """The whole months from one timestamp to the other, as a calendar counts them: negative where `to` is
        before."""

    @abstractmethod
    def seconds_between(self, from_: str, to: str) -> str:
        """The seconds from one timestamp to the other, a fraction of one among them."""

    @abstractmethod
    def truncate(self, unit: str, timestamp: str) -> str | None:
        """The timestamp truncated to the unit, one of TRUNCATION_UNITS, as Neo4j truncates: None for another unit."""

    to_char = "to_char"
    """The name of the function that writes a timestamp or a number as text in a pattern (`'YYYY-MM-DD'`,
    `'FM0000'`), as Postgres's and Oracle's `to_char` does."""

    trunc = "TRUNC"
    """The name of the function that cuts a number's fraction off, as `TRUNC` does."""

    epoch = "TIMESTAMP '1970-01-01 00:00:00'"
    """The timestamp of the epoch, 1970-01-01 at midnight."""

    def quotient(self, dividend: str, divisor: str) -> str:
        """The dividend divided by the divisor, numbers each, with its fraction kept where both are whole numbers."""
        return f"{dividend} / {divisor}"

    def extract(self, field: str, timestamp: str) -> str:
        """The field (`YEAR`, `MONTH`, `DAY`, `HOUR`, `MINUTE`, `SECOND` with its fraction) of the timestamp, a
        number."""
        return f"EXTRACT({field} FROM {timestamp})"

    def replace(self, text: str, pattern: str, replacement: str) -> str:
        """The text with every match of the pattern, a SQL text, replaced by the replacement."""
        return f"REGEXP_REPLACE({text}, {pattern}, {replacement})"

    def unzoned(self, text: str) -> str:
        """The zoned text without the offset it ends with."""
        return self.replace(text, f"'{OFFSET}'", "''")
