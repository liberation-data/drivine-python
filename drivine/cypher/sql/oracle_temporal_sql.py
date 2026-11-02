from __future__ import annotations

from collections.abc import Callable

from drivine.cypher.sql.temporal import Temporal
from drivine.cypher.sql.temporal_sql import BC_BELOW, Shared, TemporalSql, years_of

CLOCK = "CAST(SYS_EXTRACT_UTC(SYSTIMESTAMP) AS TIMESTAMP)"
"""The clock as the query that reads it starts, in UTC."""

NOW = "cy_now"
"""The variable a block reads CLOCK into as it starts."""

_FORMATS = {
    "year": "YYYY",
    "weekYear": "IYYY",
    "quarter": "Q",
    "month": "MM",
    "week": "IW",
    "day": "DD",
    "hour": "HH24",
    "minute": "MI",
}
"""`TRUNC`'s format model of each unit: `IYYY` is the first day of the ISO week-based year."""


class OracleTemporalSql(TemporalSql):
    """
    Oracle's SQL for temporal values: see TemporalSql. Oracle reads ISO 8601 text as a JSON value's moment, and has no
    time of day apart from a day, so a time of day is read on 1970-01-01. Its intervals keep months apart from days.
    `ADD_MONTHS` and `MONTHS_BETWEEN` take the last day of a month to the last day of another, where Cypher keeps the
    day of the month, so months are added and counted from the first of the month.
    """

    def __init__(self, shared: Shared, procedural: Callable[[], bool]) -> None:
        super().__init__(shared)
        self._procedural = procedural
        """Whether the statement is a block, which reads the clock into NOW as it starts."""

    def local(self, text: str, kind: Temporal) -> str:
        if kind is Temporal.DATE:
            return f"TO_TIMESTAMP({self.era_of(text)}, 'SYYYY-MM-DD')"
        if kind is Temporal.LOCAL_DATE_TIME:
            return _iso(self.era_of(text), "TIMESTAMP")
        if kind is Temporal.DATE_TIME:
            return _iso(self.era_of(text, zoned=True), "TIMESTAMP")
        if kind is Temporal.LOCAL_TIME:
            return _iso(f"'1970-01-01T' || {text}", "TIMESTAMP")
        if kind is Temporal.TIME:
            return _iso(f"'1970-01-01T' || {self.unzoned(text)}", "TIMESTAMP")
        raise ValueError("a duration is no moment")

    def utc(self, text: str, kind: Temporal) -> str:
        if kind is Temporal.DATE_TIME:
            return f"CAST(SYS_EXTRACT_UTC({_iso(self.era_of(text), 'TIMESTAMP WITH TIME ZONE')}) AS TIMESTAMP)"
        if kind is Temporal.TIME:
            time = _iso("'1970-01-01T' || " + text, "TIMESTAMP WITH TIME ZONE")
            return f"CAST(SYS_EXTRACT_UTC({time}) AS TIMESTAMP)"
        return self.local(text, kind)

    def era(self, text: str) -> str:
        """
        Oracle numbers a year BC from -1, with no year 0, in ISO text as in its own: so 1 BC, the year `0000`, is
        `-0001`, and 45 BC, the year `-0044`, is `-0045`. See `bc_only`.
        """
        return self.shared(
            [text],
            lambda t: self.bc_only(
                t[0],
                f"ASCII({t[0]}) < {BC_BELOW}",
                f"REGEXP_REPLACE({t[0]}, '^(-[0-9]{{4}}|0000)', "
                f"TO_CHAR(TO_NUMBER(REGEXP_SUBSTR({t[0]}, '^-?[0-9]{{4}}')) - 1, 'FM0000'))",
            ),
        )

    def text_year(self, text: str, kind: Temporal) -> str | None:
        """
        The digits the text opens with, and their sign; but the year of the timestamp where the text is `known_ad`,
        whose year Oracle numbers so too, as reading the timestamp's is quicker on Oracle than a regular expression.
        """
        if kind not in (Temporal.DATE, Temporal.LOCAL_DATE_TIME, Temporal.DATE_TIME):
            return None
        if self.reads_ad(text):
            return f"EXTRACT(YEAR FROM {self.local(text, kind)})"
        return f"TO_NUMBER(REGEXP_SUBSTR({text}, '^-?[0-9]+'))"

    def date(self, timestamp: str, then: str = "") -> str:
        """Where it may be BC (`bc_only`), `SYYYY` writes a year with its sign, as Oracle numbers it."""
        return self.bc_only(
            timestamp,
            f"{timestamp} < TIMESTAMP '0001-01-01 00:00:00'",
            f"REGEXP_REPLACE(LTRIM(TO_CHAR({timestamp}, 'SYYYY-MM-DD{then}')), '^-[0-9]{{4}}', "
            f"TO_CHAR(EXTRACT(YEAR FROM {timestamp}) + 1, 'FM0000'))",
            super().date(timestamp, then),
        )

    @property
    def now(self) -> str:
        """
        The clock as a query starts. A block reads it once, into its variable NOW, where each statement of the block
        would read it as that statement starts, and a statement's clock is one in Cypher.
        """
        return NOW if self._procedural() else CLOCK

    def matches(self, text: str, pattern: str) -> str:
        return f"REGEXP_LIKE({text}, {pattern})"

    def group(self, text: str, pattern: str, group: int) -> str:
        return f"REGEXP_SUBSTR({text}, {pattern}, 1, 1, NULL, {group})"

    def number(self, text: str) -> str:
        return f"TO_NUMBER({text})"

    def sortable(self, timestamp: str) -> str:
        return f"TO_CHAR({timestamp}, 'YYYY-MM-DD\"T\"HH24:MI:SS.FF6')"

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
                [f"(({self.year(t)} - {self.year(f)}) * 12 + EXTRACT(MONTH FROM {t}) - EXTRACT(MONTH FROM {f}))"],
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
        """
        `TRUNC` of a date to a format model. A millennium, a century and a decade are the year less its years into the
        span (see `years_of`), as months before the year's first day.
        """
        format_ = _FORMATS.get(unit)
        if format_ is not None:
            return f"CAST(TRUNC(CAST({timestamp} AS DATE), '{format_}') AS TIMESTAMP)"
        if unit == "second":
            return f"CAST(CAST({timestamp} AS DATE) AS TIMESTAMP)"
        if unit == "millisecond":
            return f"TO_TIMESTAMP(TO_CHAR({timestamp}, 'SYYYY-MM-DD HH24:MI:SS.FF3'), 'SYYYY-MM-DD HH24:MI:SS.FF3')"
        if unit == "microsecond":
            return timestamp
        years = years_of(unit)
        if years is not None:
            return self.shared(
                [timestamp],
                lambda t: (
                    f"CAST(ADD_MONTHS(TRUNC(CAST({t[0]} AS DATE), 'YYYY'), "
                    f"-12 * MOD({self.year(t[0])}, {years})) AS TIMESTAMP)"
                ),
            )
        return None


def _iso(text: str, type_: str) -> str:
    """The moment the ISO 8601 text writes, as Oracle reads it out of JSON, as the type."""
    return f"JSON_VALUE(JSON_SCALAR({text}), '$' RETURNING {type_})"
