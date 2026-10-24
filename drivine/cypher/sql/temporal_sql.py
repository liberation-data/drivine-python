from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

from drivine.cypher.sql.temporal import OFFSET, Temporal

Shared = Callable[[list[str], Callable[[list[str]], str]], str]
"""The compiler's `shared`: a value it is given is written once where it is read more than once."""


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
        """The timestamp truncated to the unit (`year`, `quarter`, `month`, `week`, `day`, `hour`, `minute`, `second`,
        `millisecond`): None for another unit."""

    def replace(self, text: str, pattern: str, replacement: str) -> str:
        """The text with every match of the pattern, a SQL text, replaced by the replacement."""
        return f"REGEXP_REPLACE({text}, {pattern}, {replacement})"

    def unzoned(self, text: str) -> str:
        """The zoned text without the offset it ends with."""
        return self.replace(text, f"'{OFFSET}'", "''")
