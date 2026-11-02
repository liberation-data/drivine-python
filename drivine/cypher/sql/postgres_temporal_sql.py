from __future__ import annotations

from drivine.cypher.sql.temporal import Temporal
from drivine.cypher.sql.temporal_sql import BC_BELOW, TemporalSql, years_of

_TRUNCATED = {"year", "quarter", "month", "week", "day", "hour", "minute", "second", "millisecond"}

_LIFT_YEARS = 4800
"""How many years `PostgresTemporalSql.year` lifts a timestamp by: see it."""

_LIFT = "interval '4800 years'"
"""_LIFT_YEARS as an interval."""

_FIRST_AD = "timestamp '0001-01-01'"
"""The first moment of year 1: a timestamp before it has a year BC."""


class PostgresTemporalSql(TemporalSql):
    """Postgres's SQL for temporal values: see TemporalSql. Its intervals keep months, days and time apart, as Cypher's
    durations do."""

    def local(self, text: str, kind: Temporal) -> str:
        if kind is Temporal.DATE:
            return f"CAST(CAST({self.era_of(text)} AS date) AS timestamp)"
        if kind is Temporal.LOCAL_DATE_TIME:
            return f"CAST({self.era_of(text)} AS timestamp)"
        if kind is Temporal.DATE_TIME:
            return f"CAST({self.era_of(text, zoned=True)} AS timestamp)"
        if kind is Temporal.LOCAL_TIME:
            return f"CAST('1970-01-01 ' || {text} AS timestamp)"
        if kind is Temporal.TIME:
            return f"CAST('1970-01-01 ' || {self.unzoned(text)} AS timestamp)"
        raise ValueError("a duration is no moment")

    def utc(self, text: str, kind: Temporal) -> str:
        if kind is Temporal.DATE_TIME:
            return f"(CAST({self.era_of(text)} AS timestamptz) AT TIME ZONE 'UTC')"
        if kind is Temporal.TIME:
            return f"(CAST('1970-01-01 ' || {text} AS timestamptz) AT TIME ZONE 'UTC')"
        return self.local(text, kind)

    def era(self, text: str) -> str:
        """
        Postgres reads no year before 1 in ISO text, but reads one BC after it: so 1 BC, the year `0000`, is
        `0001 … BC`, and 45 BC, the year `-0044`, is `0045 … BC`. See `bc_only`.
        """
        return self.shared(
            [text],
            lambda t: self.bc_only(
                t[0],
                f"ascii({t[0]}) < {BC_BELOW}",
                f"regexp_replace({t[0]}, '^(-[0-9]{{4}}|0000)(.*)$', "
                f"to_char(1 - CAST(substring({t[0]} from '^-?[0-9]{{4}}') AS int), 'FM0000') || '\\2 BC')",
            ),
        )

    def date(self, timestamp: str, then: str = "") -> str:
        """Where it may be BC (`bc_only`), the year as `year` numbers it, written with its sign as `to_char` writes a
        negative number, then the rest of the date."""
        return self.bc_only(
            timestamp,
            f"{timestamp} < {_FIRST_AD}",
            f"to_char({self.year(timestamp)}, 'FM0000') || to_char({timestamp}, '-MM-DD{then}')",
            super().date(timestamp, then),
        )

    def year(self, timestamp: str) -> str:
        """
        The year of the timestamp _LIFT years later, less _LIFT_YEARS: _LIFT is twelve cycles of the Gregorian
        calendar's 400 years, after which its days, leap days and ISO weeks fall again as they did, and lifts Postgres's
        first year, 4713 BC, past 0. So a year BC is numbered as Cypher numbers it, the timestamp read once, where
        Postgres numbers it from -1.
        """
        return f"(EXTRACT(YEAR FROM ({timestamp}) + {_LIFT}) - {_LIFT_YEARS})"

    def text_year(self, text: str, kind: Temporal) -> str | None:
        """The date's text, its time of day cut off, less its month and day."""
        if kind is Temporal.DATE:
            return f"CAST(left({text}, -6) AS numeric)"
        if kind in (Temporal.LOCAL_DATE_TIME, Temporal.DATE_TIME):
            return f"CAST(left(split_part({text}, 'T', 1), -6) AS numeric)"
        return None

    def week_year(self, timestamp: str) -> str:
        """The ISO week's year, lifted as `year` lifts it: `to_char` writes a week's year BC unsigned."""
        week = self.number(f"to_char(({timestamp}) + {_LIFT}, 'IYYY')")
        return f"({week} - {_LIFT_YEARS})"

    @property
    def now(self) -> str:
        return "(statement_timestamp() AT TIME ZONE 'UTC')"

    def matches(self, text: str, pattern: str) -> str:
        return f"({text} ~ {pattern})"

    def group(self, text: str, pattern: str, group: int) -> str:
        return f"(regexp_match({text}, {pattern}))[{group}]"

    def number(self, text: str) -> str:
        return f"CAST(NULLIF({text}, '') AS numeric)"

    def sortable(self, timestamp: str) -> str:
        return f"to_char({timestamp}, 'YYYY-MM-DD\"T\"HH24:MI:SS.US')"

    def micros(self, timestamp: str) -> str:
        return f"to_char({timestamp}, 'US')"

    def day_of_week(self, timestamp: str) -> str:
        return f"CAST(EXTRACT(ISODOW FROM {timestamp}) AS numeric)"

    def add_months(self, timestamp: str, months: str) -> str:
        return f"({timestamp} + make_interval(months => CAST({months} AS int)))"

    def add_time(self, timestamp: str, days: str, seconds: str) -> str:
        return (
            f"({timestamp} + make_interval(days => CAST({days} AS int), secs => CAST({seconds} AS double precision)))"
        )

    def months_between(self, from_: str, to: str) -> str:
        """`age` counts in calendar months, then days, then time, as Cypher's `duration.between` does."""
        return self.shared(
            [f"age({to}, {from_})"], lambda it: f"(EXTRACT(YEAR FROM {it[0]}) * 12 + EXTRACT(MONTH FROM {it[0]}))"
        )

    def seconds_between(self, from_: str, to: str) -> str:
        return f"EXTRACT(EPOCH FROM ({to} - {from_}))"

    def truncate(self, unit: str, timestamp: str) -> str | None:
        """
        `date_trunc`, but for the units it counts otherwise than Neo4j: its millennium starts in 2001 and its century in
        1901, so those and the decade are the year less its years into the span (see `years_of`). The week-based year
        is so many weeks before the timestamp's week, its ISO week less one.
        """
        if unit in _TRUNCATED:
            return f"date_trunc('{unit}', {timestamp})"
        if unit == "microsecond":
            return f"date_trunc('microseconds', {timestamp})"
        years = years_of(unit)
        if years is not None:
            return self.shared(
                [timestamp],
                lambda t: (
                    f"(date_trunc('year', {t[0]}) - make_interval(years => MOD(CAST({self.year(t[0])} AS int), "
                    f"{years})))"
                ),
            )
        if unit == "weekYear":
            return self.shared(
                [timestamp],
                lambda t: (
                    f"(date_trunc('week', {t[0]}) - make_interval(weeks => CAST(EXTRACT(WEEK FROM {t[0]}) AS int) - 1))"
                ),
            )
        return None
