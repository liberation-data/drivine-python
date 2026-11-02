from __future__ import annotations

import re
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


BC_BELOW = 49
"""
The code of `1`: an ISO text whose first character's code is below it starts with `-` or `0`, as one with a year BC
does, so a store's `era` rewrites it (`bc_only`), and reads any other, an ordinary date, as it is.
"""

GUARDED_UP_TO = 200
"""The longest value `bc_only` tests: as long as the compiler writes a value where it is read, unbound."""

_BOUND = re.compile(r"\bsh[0-9]+[.]v")
"""A name of a value the compiler binds in a subquery, `sh2.v1`: see `SqlCypherCompiler.bound`."""


class TemporalSql(ABC):
    """
    A store's SQL for temporal values, which Temporals works openCypher's out of. Its texts and timestamps are SQL: a
    text is a value's ISO 8601 text, a timestamp a SQL timestamp of no zone, a number a SQL number. `shared` is the
    compiler's: a value it is given is written once where it is read more than once.
    """

    def __init__(self, shared: Shared) -> None:
        self.shared = shared
        self._ad: list[str] = []
        """The texts `known_ad` has been told are AD, where the SQL it writes reads them."""
        self._read_ad = False
        """Whether `era_of` has read a text `known_ad` was told is AD, since the `known_ad` it is in began."""

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

    @property
    def numbers_bc_from_one(self) -> bool:
        """
        Whether the store numbers a year BC from -1, with no year 0, as Postgres and Oracle do, where Cypher numbers
        years astronomically: 0 for 1 BC and -44 for 45 BC. Where it does, a year before 1 is one more than the store's.
        """
        return True

    def year(self, timestamp: str) -> str:
        """The year of the timestamp, a number, as Cypher numbers it: see `numbers_bc_from_one`."""
        year = f"EXTRACT(YEAR FROM {timestamp})"
        if not self.numbers_bc_from_one:
            return year
        return self.shared([year], lambda y: f"({y[0]} + CASE WHEN {y[0]} < 0 THEN 1 ELSE 0 END)")

    def text_year(self, text: str, kind: Temporal) -> str | None:
        """
        The year of the ISO 8601 text of the kind, a date or a moment, a number, read off the text: the digits before
        its month, with their sign, are the year as Cypher numbers it, so the text needs no `era` nor a cast to give it.
        None for a time of day, and where the store reads the year off the timestamp, as it numbers years so already.
        """
        return None

    def week_year(self, timestamp: str) -> str:
        """The year of the ISO week of the timestamp, a number, as Cypher numbers a year: `to_char` writes one BC
        unsigned."""

        def week(t: str) -> str:
            return self.number(f"to_char({t}, 'IYYY')")

        if not self.numbers_bc_from_one:
            return week(timestamp)
        return self.shared(
            [timestamp],
            lambda t: f"CASE WHEN EXTRACT(YEAR FROM {t[0]}) < 0 THEN 1 - {week(t[0])} ELSE {week(t[0])} END",
        )

    def date(self, timestamp: str, then: str = "") -> str:
        """
        The date of the timestamp as ISO 8601 writes it, `2024-03-15`, followed by the `to_char` pattern `then`: its
        year as Cypher numbers it, `0000` for 1 BC and `-0044` for 45 BC. A store's own, and its `era`, read their
        operand twice at most: a value an aggregate's argument reads is bound nowhere, and is written out again wherever
        it is read.
        """
        return f"to_char({timestamp}, 'YYYY-MM-DD{then}')"

    def era(self, text: str) -> str:
        """
        The ISO 8601 text of a date or a moment, its year numbered as Cypher numbers it, as the store reads one: itself,
        where the store numbers years so too, or the year is after 0. A store's own reads the text more than once, so
        binds it with `shared`: written out twice, a text that is itself a truncation or a value made of fields tripled
        the statement.
        """
        return text

    def known_ad(self, texts: list[str], block: Callable[[], str]) -> tuple[str, bool]:
        """
        The SQL `block` writes, reading each of `texts` as a text of a year from 1000 on, as a store reads ISO 8601:
        where a test the statement makes first, a kind's `Temporal.ad_pattern`, has told it so. `era_of` then reads such
        a text as it is, so an ordinary date costs neither `era`'s rewrite nor its test. And whether it read one so:
        where it did not, the SQL is what `block` writes anyway.
        """
        outer = self._read_ad
        self._read_ad = False
        self._ad.extend(texts)
        try:
            sql = block()
            return sql, self._read_ad
        finally:
            del self._ad[len(self._ad) - len(texts) :]
            self._read_ad = outer or self._read_ad

    def as_ad(self, of: str, derived: list[str], block: Callable[[], str]) -> str:
        """
        The SQL `block` writes, each of `derived` `known_ad` in it where `of` is: a value worked out of `of` whose year
        is after 0 where `of`'s is from 1000 on, as a truncation's, or `of` itself bound to a name. Not always from 1000
        on itself (the week-based year of `1000-01-01` starts on `0999-12-30`), but a store writes and reads every year
        after 0 as ISO 8601 does.
        """
        return self.known_ad(derived, block)[0] if self.reads_ad(of) else block()

    def reads_ad(self, text: str) -> bool:
        """Whether the text is `known_ad` where the SQL being written reads it, which it then reads as a store reads
        ISO 8601."""
        read = text in self._ad
        if read:
            self._read_ad = True
        return read

    def era_of(self, text: str, zoned: bool = False) -> str:
        """`era` of the text, `unzoned` first where `zoned`: the text as it is where it is `known_ad`."""
        local = self.unzoned(text) if zoned else text
        return local if self.reads_ad(text) else self.era(local)

    def bc_only(self, value: str, test: str, bc: str, ad: str | None = None) -> str:
        """
        `bc`, what a store reads or writes the value as where its year may be BC, where `test`, a cheap test of the
        value, says it may be; and `ad` (the value itself where None), how it reads or writes an ordinary one,
        otherwise, so an ordinary date costs `test` and no more. A long value is one written out where it is read, as in
        an aggregate's argument, which binds nothing: then `bc` alone, which reads it twice where the test reads it four
        times, so a value read again and again doubles at each reading, not quadruples. A value that reads one the
        compiler bound in a subquery (`_BOUND`) is one it worked out, not one read from the store, and is not tested
        either: Oracle plans a statement whose subqueries test such values a great deal slower (in drivine4j, the min
        and max of a list of moments worked out item by item took 12 seconds to plan, 4 without). A value `known_ad` is
        `ad` alone.
        """
        otherwise = value if ad is None else ad
        if self.reads_ad(value):
            return otherwise
        if len(value) > GUARDED_UP_TO or value == "NULL" or _BOUND.search(value):
            return bc
        return f"CASE WHEN {test} THEN {bc} ELSE {otherwise} END"

    def replace(self, text: str, pattern: str, replacement: str) -> str:
        """The text with every match of the pattern, a SQL text, replaced by the replacement."""
        return f"REGEXP_REPLACE({text}, {pattern}, {replacement})"

    def unzoned(self, text: str) -> str:
        """The zoned text without the offset it ends with."""
        return self.replace(text, f"'{OFFSET}'", "''")
