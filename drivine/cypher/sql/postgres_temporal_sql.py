from __future__ import annotations

from drivine.cypher.sql.temporal import Temporal
from drivine.cypher.sql.temporal_sql import TemporalSql

_TRUNCATED = {"year", "quarter", "month", "week", "day", "hour", "minute", "second", "millisecond"}


class PostgresTemporalSql(TemporalSql):
    """Postgres's SQL for temporal values: see TemporalSql. Its intervals keep months, days and time apart, as Cypher's
    durations do."""

    def local(self, text: str, kind: Temporal) -> str:
        if kind is Temporal.DATE:
            return f"CAST(CAST({text} AS date) AS timestamp)"
        if kind is Temporal.LOCAL_DATE_TIME:
            return f"CAST({text} AS timestamp)"
        if kind is Temporal.DATE_TIME:
            return f"CAST({self.unzoned(text)} AS timestamp)"
        if kind is Temporal.LOCAL_TIME:
            return f"CAST('1970-01-01 ' || {text} AS timestamp)"
        if kind is Temporal.TIME:
            return f"CAST('1970-01-01 ' || {self.unzoned(text)} AS timestamp)"
        raise ValueError("a duration is no moment")

    def utc(self, text: str, kind: Temporal) -> str:
        if kind is Temporal.DATE_TIME:
            return f"(CAST({text} AS timestamptz) AT TIME ZONE 'UTC')"
        if kind is Temporal.TIME:
            return f"(CAST('1970-01-01 ' || {text} AS timestamptz) AT TIME ZONE 'UTC')"
        return self.local(text, kind)

    @property
    def now(self) -> str:
        return "(statement_timestamp() AT TIME ZONE 'UTC')"

    def matches(self, text: str, pattern: str) -> str:
        return f"({text} ~ {pattern})"

    def group(self, text: str, pattern: str, group: int) -> str:
        return f"(regexp_match({text}, {pattern}))[{group}]"

    def number(self, text: str) -> str:
        return f"CAST(NULLIF({text}, '') AS numeric)"

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
        return f"date_trunc('{unit}', {timestamp})" if unit in _TRUNCATED else None
