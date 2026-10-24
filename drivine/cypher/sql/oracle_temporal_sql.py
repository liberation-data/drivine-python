from __future__ import annotations

from drivine.cypher.sql.temporal import Temporal
from drivine.cypher.sql.temporal_sql import TemporalSql

_FORMATS = {"year": "YYYY", "quarter": "Q", "month": "MM", "week": "IW", "day": "DD", "hour": "HH24", "minute": "MI"}


class OracleTemporalSql(TemporalSql):
    """
    Oracle's SQL for temporal values: see TemporalSql. Oracle reads ISO 8601 text as a JSON value's moment, and has no
    time of day apart from a day, so a time of day is read on 1970-01-01. Its intervals keep months apart from days.
    `ADD_MONTHS` and `MONTHS_BETWEEN` take the last day of a month to the last day of another, where Cypher keeps the
    day of the month, so months are added and counted from the first of the month.
    """

    def local(self, text: str, kind: Temporal) -> str:
        if kind is Temporal.DATE:
            return f"TO_TIMESTAMP({text}, 'YYYY-MM-DD')"
        if kind is Temporal.LOCAL_DATE_TIME:
            return _iso(text, "TIMESTAMP")
        if kind is Temporal.DATE_TIME:
            return _iso(self.unzoned(text), "TIMESTAMP")
        if kind is Temporal.LOCAL_TIME:
            return _iso(f"'1970-01-01T' || {text}", "TIMESTAMP")
        if kind is Temporal.TIME:
            return _iso(f"'1970-01-01T' || {self.unzoned(text)}", "TIMESTAMP")
        raise ValueError("a duration is no moment")

    def utc(self, text: str, kind: Temporal) -> str:
        if kind is Temporal.DATE_TIME:
            return f"CAST(SYS_EXTRACT_UTC({_iso(text, 'TIMESTAMP WITH TIME ZONE')}) AS TIMESTAMP)"
        if kind is Temporal.TIME:
            time = _iso("'1970-01-01T' || " + text, "TIMESTAMP WITH TIME ZONE")
            return f"CAST(SYS_EXTRACT_UTC({time}) AS TIMESTAMP)"
        return self.local(text, kind)

    @property
    def now(self) -> str:
        return "CAST(SYS_EXTRACT_UTC(SYSTIMESTAMP) AS TIMESTAMP)"

    def matches(self, text: str, pattern: str) -> str:
        return f"REGEXP_LIKE({text}, {pattern})"

    def group(self, text: str, pattern: str, group: int) -> str:
        return f"REGEXP_SUBSTR({text}, {pattern}, 1, 1, NULL, {group})"

    def number(self, text: str) -> str:
        return f"TO_NUMBER({text})"

    def micros(self, timestamp: str) -> str:
        return f"TO_CHAR({timestamp}, 'FF6')"

    def day_of_week(self, timestamp: str) -> str:
        return self.shared(
            [timestamp], lambda it: f"(TRUNC(CAST({it[0]} AS DATE)) - TRUNC(CAST({it[0]} AS DATE), 'IW') + 1)"
        )

    def add_months(self, timestamp: str, months: str) -> str:
        """
        The first of the month so many months later, by `ADD_MONTHS`, which takes a day; then the day of the month, or
        the month's last where it has fewer, and the time of day with the fraction of a second.
        """

        def added(values: list[str]) -> str:
            t, m = values
            return self.shared(
                [f"ADD_MONTHS(TRUNC(CAST({t} AS DATE), 'MM'), {m})"],
                lambda first: (
                    f"(CAST({first[0]} AS TIMESTAMP) + NUMTODSINTERVAL(LEAST(EXTRACT(DAY FROM {t}), "
                    f"EXTRACT(DAY FROM LAST_DAY({first[0]}))) - 1, 'DAY') + "
                    f"({t} - CAST(TRUNC(CAST({t} AS DATE)) AS TIMESTAMP)))"
                ),
            )

        return self.shared([timestamp, months], added)

    def add_time(self, timestamp: str, days: str, seconds: str) -> str:
        return f"({timestamp} + NUMTODSINTERVAL({days}, 'DAY') + NUMTODSINTERVAL({seconds}, 'SECOND'))"

    def months_between(self, from_: str, to: str) -> str:
        """The months between the calendar months, less one where `to`'s day and time of the month are short of
        `from_`'s."""

        def between(values: list[str]) -> str:
            f, t = values
            since, until = (f"TO_CHAR({it}, 'DDHH24MISSFF6')" for it in (f, t))
            return self.shared(
                [
                    f"((EXTRACT(YEAR FROM {t}) - EXTRACT(YEAR FROM {f})) * 12 + EXTRACT(MONTH FROM {t}) - "
                    f"EXTRACT(MONTH FROM {f}))"
                ],
                lambda m: (
                    f"({m[0]} - CASE WHEN {m[0]} > 0 AND {until} < {since} THEN 1 "
                    f"WHEN {m[0]} < 0 AND {until} > {since} THEN -1 ELSE 0 END)"
                ),
            )

        return self.shared([from_, to], between)

    def seconds_between(self, from_: str, to: str) -> str:
        def between(values: list[str]) -> str:
            interval = f"({values[1]} - {values[0]})"
            return (
                f"(EXTRACT(DAY FROM {interval}) * 86400 + EXTRACT(HOUR FROM {interval}) * 3600 + "
                f"EXTRACT(MINUTE FROM {interval}) * 60 + EXTRACT(SECOND FROM {interval}))"
            )

        return self.shared([from_, to], between)

    def truncate(self, unit: str, timestamp: str) -> str | None:
        format_ = _FORMATS.get(unit)
        if format_ is not None:
            return f"CAST(TRUNC(CAST({timestamp} AS DATE), '{format_}') AS TIMESTAMP)"
        if unit == "second":
            return f"CAST(CAST({timestamp} AS DATE) AS TIMESTAMP)"
        if unit == "millisecond":
            return f"TO_TIMESTAMP(TO_CHAR({timestamp}, 'YYYY-MM-DD HH24:MI:SS.FF3'), 'YYYY-MM-DD HH24:MI:SS.FF3')"
        return None


def _iso(text: str, type_: str) -> str:
    """The moment the ISO 8601 text writes, as Oracle reads it out of JSON, as the type."""
    return f"JSON_VALUE(JSON_SCALAR({text}), '$' RETURNING {type_})"
