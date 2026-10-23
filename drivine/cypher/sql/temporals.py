from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import ROUND_DOWN, Decimal, InvalidOperation, localcontext

from drivine.cypher.sql.temporal import DURATION_PARTS, INSTANTS, OFFSET, Temporal
from drivine.cypher.sql.temporal_sql import Shared, TemporalSql


class NoNumber(ValueError):
    """A part of a duration's text that is no number (`P1.2.3D`)."""


@dataclass(frozen=True)
class Parts:
    """A duration's parts, SQL numbers each: whole months, whole days, and seconds with their fraction."""

    months: str
    days: str
    seconds: str


@dataclass(frozen=True)
class Field:
    """A field of a value: its SQL, and whether it is a text, where it is not a number."""

    sql: str
    text: bool


FIELD_NAMES = frozenset(
    {
        "year", "quarter", "month", "week", "weekYear", "day", "ordinalDay", "dayOfWeek", "weekDay", "hour", "minute",
        "second", "millisecond", "microsecond", "nanosecond", "epochSeconds", "epochMillis", "timezone", "offset",
        "offsetMinutes", "offsetSeconds", "years", "quarters", "months", "weeks", "days", "hours", "minutes", "seconds",
        "milliseconds", "microseconds", "nanoseconds", "quartersOfYear", "monthsOfQuarter", "monthsOfYear",
        "daysOfWeek", "minutesOfHour", "secondsOfMinute", "millisecondsOfSecond", "microsecondsOfSecond",
        "nanosecondsOfSecond",
    }
)  # fmt: skip
"""The names of the fields a temporal value may have, of a moment's or a duration's."""

INSTANT_FIELDS = frozenset(
    {
        "year", "quarter", "month", "week", "weekYear", "day", "ordinalDay", "dayOfWeek", "weekDay", "hour", "minute",
        "second", "millisecond", "microsecond", "nanosecond",
    }
)  # fmt: skip
"""The fields of a moment or a time of day read off its wall clock."""

_TIMES = (Temporal.TIME, Temporal.LOCAL_TIME)
"""The kinds of a time of day, which no months are between."""

_Body = Callable[[Temporal, str], str]
"""What a value of a kind is, of the kind and the value's text."""


class Temporals:
    """
    openCypher's temporal values on a SQL store, worked out of a store's TemporalSql. A value is its ISO 8601 text, as a
    store keeps a moment: `2024-03-01`, `10:30:00`, `10:30:00+02:00`, `2024-03-01T10:30:00`,
    `2024-03-01T10:30:00.500Z`, `P1Y2M3DT4H`. A zoned value keeps the offset it was written with, its fields read on its
    wall clock and compared as the moment it is. A duration is months, days and seconds, as Cypher's is, and as a
    Postgres interval is.

    Every argument and result is SQL: a text, a number, or with `shared` a value written once where it is read more than
    once.
    """

    def __init__(self, sql: TemporalSql, shared: Shared) -> None:
        self.sql = sql
        self.shared = shared

    # ----- Kinds -----

    def is_kind(self, text: str, kind: Temporal) -> str:
        """Whether the text is of the kind."""
        return self.sql.matches(text, _literal(kind.pattern))

    def by_kind(self, text: str, body: _Body, kinds: tuple[Temporal, ...] = INSTANTS) -> str:
        """The body of the kind among `kinds` that the text is, as its shape tells, and of the text, written once: null
        where it is none."""
        return self.shared(
            [text],
            lambda t: f"CASE {' '.join(f'WHEN {self.is_kind(t[0], it)} THEN {body(it, t[0])}' for it in kinds)} END",
        )

    # ----- Moments and times of day -----

    def format(self, timestamp: str, kind: Temporal, offset: str | None = None) -> str:
        """
        The text of the kind the wall clock `timestamp` reads, with the offset, a text, where the kind is zoned: null
        where the timestamp is null, which Oracle would otherwise write out as the text's punctuation alone (`.`, `.Z`),
        for its null joins a text as the empty text.
        """

        def formatted(values: list[str]) -> str:
            t = values[0]
            clock = f"to_char({t}, 'HH24:MI:SS') || {self._fraction(t)}"
            zone = offset if offset is not None else "'Z'"
            if kind is Temporal.DATE:
                return f"to_char({t}, 'YYYY-MM-DD')"
            if kind is Temporal.LOCAL_DATE_TIME:
                text = f"to_char({t}, 'YYYY-MM-DD\"T\"') || {clock}"
            elif kind is Temporal.DATE_TIME:
                text = f"to_char({t}, 'YYYY-MM-DD\"T\"') || {clock} || {zone}"
            elif kind is Temporal.LOCAL_TIME:
                text = clock
            elif kind is Temporal.TIME:
                text = f"{clock} || {zone}"
            else:
                raise ValueError("a duration is no moment")
            return f"CASE WHEN {t} IS NOT NULL THEN {text} END"

        return self.shared([timestamp], formatted)

    def _fraction(self, timestamp: str) -> str:
        """A second's fraction as Java writes one: none for a whole second, else three digits or six."""
        return self.shared(
            [self.sql.micros(timestamp)],
            lambda f: (
                f"CASE WHEN {f[0]} = '000000' THEN '' WHEN SUBSTR({f[0]}, 4, 3) = '000' "
                f"THEN '.' || SUBSTR({f[0]}, 1, 3) ELSE '.' || {f[0]} END"
            ),
        )

    def offset(self, text: str) -> str:
        """
        The offset the zoned text ends with, as Cypher gives it: with a colon where it is written without one, and `Z`
        where it is none, however written (`+00:00`, `-0000`), so a value at UTC is equal to another however each
        writes it.
        """
        utc = self.sql.replace(self.sql.group(text, _literal(OFFSET), 1), "'^[+-]00:?00$'", "'Z'")
        return self.sql.replace(utc, "'^([+-][0-9]{2})([0-9]{2})$'", "'\\1:\\2'")

    def _offset_of(self, text: str, from_: Temporal, to: Temporal) -> str | None:
        """The offset a value of `to` made of the text of `from_` has: its own, or UTC's where it has none; none where
        `to` is local."""
        if not to.zoned:
            return None
        return self.offset(text) if from_.zoned else "'Z'"

    def convert(self, text: str, from_: Temporal, to: Temporal) -> str:
        """The text of `to` the text of `from_` is: a moment's date, a date's midnight, a local value's in UTC."""
        return self.shared(
            [text], lambda t: self.format(self.sql.local(t[0], from_), to, self._offset_of(t[0], from_, to))
        )

    def convert_any(self, text: str, to: Temporal) -> str:
        """The text of `to` the text is, whatever kind its shape tells: null where it is none."""
        return self.by_kind(text, lambda from_, t: self.convert(t, from_, to))

    def now(self, kind: Temporal) -> str:
        """The text of the kind the statement runs at, in UTC."""
        return self.format(self.sql.now, kind)

    def of(self, fields: dict[str, str], timezone: str | None, kind: Temporal) -> str:
        """
        The text of the kind of the fields, SQL numbers each by Cypher's names (`year`, `month`, `day`, `hour`,
        `minute`, `second`, `millisecond`, `microsecond`, `nanosecond`), and the timezone, a text.
        """

        def field(name: str, default: str, digits: int) -> str:
            return f"to_char({fields.get(name, default)}, 'FM{'0' * digits}')"

        micros = (
            f"TRUNC({fields.get('millisecond', '0')} * 1000 + {fields.get('microsecond', '0')} + "
            f"{fields.get('nanosecond', '0')} / 1000)"
        )
        day = f"{field('year', '1970', 4)} || '-' || {field('month', '1', 2)} || '-' || {field('day', '1', 2)}"
        clock = (
            f"{field('hour', '0', 2)} || ':' || {field('minute', '0', 2)} || ':' || {field('second', '0', 2)} "
            "|| '.' || "
            f"to_char({micros}, 'FM000000')"
        )
        zone = timezone if timezone is not None else "'Z'"
        if kind is Temporal.DATE:
            text = day
        elif kind is Temporal.LOCAL_DATE_TIME:
            text = f"{day} || 'T' || {clock}"
        elif kind is Temporal.DATE_TIME:
            text = f"{day} || 'T' || {clock} || {zone}"
        elif kind is Temporal.LOCAL_TIME:
            text = clock
        elif kind is Temporal.TIME:
            text = f"{clock} || {zone}"
        else:
            raise ValueError("a duration is made of its own fields")
        return self.convert(text, kind, kind)

    def field(self, text: str, kind: Temporal, name: str) -> Field | None:
        """
        A field of the text of the kind, by Cypher's name for it: a number, or for `timezone` and `offset` a text; None
        for a name the kind has no field of. A zoned value's fields are those of its wall clock.
        """
        if kind is Temporal.DURATION:
            if _duration_field(self._parts("NULL"), name) is None:
                return None
            return Field(self.shared([text], lambda t: _duration_field(self._parts(t[0]), name) or ""), text=False)
        if kind.zoned and name in ("timezone", "offset", "offsetMinutes", "offsetSeconds"):
            return self._zone_field(text, name)
        epoch = "TIMESTAMP '1970-01-01 00:00:00'"
        if name == "epochSeconds":
            return Field(f"TRUNC({self.sql.seconds_between(epoch, self.sql.utc(text, kind))})", text=False)
        if name == "epochMillis":
            return Field(f"TRUNC({self.sql.seconds_between(epoch, self.sql.utc(text, kind))} * 1000)", text=False)
        if name not in INSTANT_FIELDS:
            return None

        def number(values: list[str]) -> str:
            t = values[0]
            second = f"EXTRACT(SECOND FROM {t})"
            if name == "year":
                return f"EXTRACT(YEAR FROM {t})"
            if name == "quarter":
                return f"TRUNC((EXTRACT(MONTH FROM {t}) + 2) / 3)"
            if name == "month":
                return f"EXTRACT(MONTH FROM {t})"
            if name == "week":
                return self.sql.number(f"to_char({t}, 'IW')")
            if name == "weekYear":
                return self.sql.number(f"to_char({t}, 'IYYY')")
            if name == "day":
                return f"EXTRACT(DAY FROM {t})"
            if name == "ordinalDay":
                return self.sql.number(f"to_char({t}, 'DDD')")
            if name in ("dayOfWeek", "weekDay"):
                return self.sql.day_of_week(t)
            if name == "hour":
                return f"EXTRACT(HOUR FROM {t})"
            if name == "minute":
                return f"EXTRACT(MINUTE FROM {t})"
            if name == "second":
                return f"TRUNC({second})"
            if name == "millisecond":
                return f"(TRUNC({second} * 1000) - TRUNC({second}) * 1000)"
            if name == "microsecond":
                return f"(TRUNC({second} * 1000000) - TRUNC({second}) * 1000000)"
            return f"((TRUNC({second} * 1000000) - TRUNC({second}) * 1000000) * 1000)"

        return Field(self.shared([self.sql.local(text, kind)], number), text=False)

    def _zone_field(self, text: str, name: str) -> Field:
        def zone(values: list[str]) -> str:
            o = values[0]
            sign = f"CASE WHEN SUBSTR({o}, 1, 1) = '-' THEN -1 ELSE 1 END"
            hours = self.sql.number(f"SUBSTR({o}, 2, 2)")
            minutes_of_hour = self.sql.number(f"SUBSTR(REPLACE({o}, ':', ''), 4, 2)")
            minutes = f"CASE WHEN {o} = 'Z' THEN 0 ELSE {sign} * ({hours} * 60 + {minutes_of_hour}) END"
            if name in ("timezone", "offset"):
                return o
            if name == "offsetMinutes":
                return minutes
            return f"({minutes}) * 60"

        return Field(self.shared([self.offset(text)], zone), text=name in ("timezone", "offset"))

    def add(self, text: str, kind: Temporal, duration: str, sign: int) -> str:
        """The text of the kind the duration text after the text of it, or with `sign` -1 before: a zoned value keeps
        its offset."""

        def added(values: list[str]) -> str:
            t, d = values
            moved = self._moved(self.sql.local(t, kind), d, sign, "1 = 1" if kind is Temporal.DATE else None)
            return self.format(moved, kind, self._offset_of(t, kind, kind))

        return self.shared([text, duration], added)

    def add_to_any(self, text: str, duration: str, sign: int) -> str:
        """
        `add` for a text of whatever kind its shape tells: read as a wall clock once, whatever its kind, the duration
        added once, and written as the kind it was.
        """

        def added(values: list[str]) -> str:
            t, d = values
            clock = (
                f"CASE {' '.join(f'WHEN {self.is_kind(t, it)} THEN {self.sql.local(t, it)}' for it in INSTANTS)} END"
            )
            return self.shared(
                [self._moved(clock, d, sign, self.is_kind(t, Temporal.DATE))],
                lambda moved: (
                    "CASE "
                    + " ".join(
                        f"WHEN {self.is_kind(t, it)} THEN {self.format(moved[0], it, self._offset_of(t, it, it))}"
                        for it in INSTANTS
                    )
                    + " END"
                ),
            )

        return self.shared([text, duration], added)

    def _moved(self, timestamp: str, duration: str, sign: int, date_: str | None = None) -> str:
        """
        The timestamp the duration text later, or with `sign` -1 earlier. Where `date_`, a SQL condition, holds, the
        duration's time is taken in whole days alone, as Cypher adds one to a date: a date less `PT1H` is the same date,
        less `PT25H` the day before.
        """
        s = "-" if sign < 0 else ""
        parts = self._parts(duration)
        if date_ is None:
            seconds = parts.seconds
        else:
            seconds = self.shared(
                [parts.seconds], lambda sec: f"CASE WHEN {date_} THEN TRUNC({sec[0]} / 86400) * 86400 ELSE {sec[0]} END"
            )
        return self.sql.add_time(
            self.sql.add_months(timestamp, f"{s}({parts.months})"), f"{s}({parts.days})", f"{s}({seconds})"
        )

    def truncate(self, unit: str, text: str, from_: Temporal, to: Temporal) -> str | None:
        """The text of `to` the text of `from_` is, cut short to the unit: None where the store truncates to no such
        unit."""
        if self.sql.truncate(unit, "NULL") is None:
            return None
        return self.shared(
            [text],
            lambda t: self.format(
                self.sql.truncate(unit, self.sql.local(t[0], from_)) or "", to, self._offset_of(t[0], from_, to)
            ),
        )

    def moment(self, text: str, kind: Temporal) -> str:
        """What the text of the kind sorts by: the moment it is, whatever its offset, as a timestamp."""
        return self.sql.utc(text, kind)

    def compare(self, a: str, b: str, kind: Temporal, operator: str) -> str:
        """Whether the text `a` stands to the text `b`, both of the kind, as the operator says: null where either is no
        such text."""

        def compared(values: list[str]) -> str:
            x, y = values
            if kind is Temporal.DURATION:
                # A duration is equal to another of the same parts, and is in no order.
                if operator not in ("=", "<>"):
                    test = "NULL"
                else:
                    p, q = self._parts(x), self._parts(y)
                    equal = f"({p.months} = {q.months} AND {p.days} = {q.days} AND {p.seconds} = {q.seconds})"
                    test = equal if operator == "=" else f"NOT {equal}"
            elif kind.zoned and operator in ("=", "<>"):
                # Two zoned values are equal where they are the same moment at the same offset, as Neo4j has them.
                equal = f"({self.sql.utc(x, kind)} = {self.sql.utc(y, kind)} AND {self.offset(x)} = {self.offset(y)})"
                test = equal if operator == "=" else f"NOT {equal}"
            else:
                test = f"{self.sql.utc(x, kind)} {operator} {self.sql.utc(y, kind)}"
            return f"CASE WHEN {self.is_kind(x, kind)} AND {self.is_kind(y, kind)} THEN {test} END"

        return self.shared([a, b], compared)

    # ----- Durations -----

    def _parts(self, text: str) -> Parts:
        """
        The parts of the duration text, which each read it more than once: the text is a name, written once. A part it
        leaves out is 0, and every part of null is null, so a duration of null is added as null. A duration negated
        whole opens with its sign.
        """

        def part(name: str) -> str:
            pattern, group = DURATION_PARTS[name]
            return f"COALESCE({self.sql.number(self.sql.group(text, _literal(pattern), group))}, 0)"

        sign = f"CASE WHEN SUBSTR({text}, 1, 1) = '-' THEN -1 WHEN {text} IS NOT NULL THEN 1 END"
        return Parts(
            f"({sign} * ({part('years')} * 12 + {part('months')}))",
            f"({sign} * ({part('weeks')} * 7 + {part('days')}))",
            f"({sign} * ({part('hours')} * 3600 + {part('minutes')} * 60 + {part('seconds')}))",
        )

    def duration(self, fields: dict[str, str]) -> str:
        """The duration text of the fields, SQL numbers each by Cypher's names (`years`, `months`, `weeks`, `days`,
        `hours`…)."""

        def f(name: str) -> str:
            return fields.get(name, "0")

        return self._text(
            Parts(
                f"({f('years')} * 12 + {f('quarters')} * 3 + {f('months')})",
                f"({f('weeks')} * 7 + {f('days')})",
                f"({f('hours')} * 3600 + {f('minutes')} * 60 + {f('seconds')} + {f('milliseconds')} / 1000 + "
                f"{f('microseconds')} / 1000000 + {f('nanoseconds')} / 1000000000)",
            )
        )

    def _text(self, parts: Parts) -> str:
        """
        The duration text of the parts, as Neo4j writes one: whole years and months, days, then hours, minutes and
        seconds, each signed, and `PT0S` for none. A fraction of a month or a day goes to the parts below it, as
        Cypher's does: a month as 30.436875 days. Null where a part is null.
        """

        def spread(values: list[str]) -> str:
            m0, d0, s0 = values
            m = f"TRUNC({m0})"
            d = f"TRUNC({d0} + ({m0} - TRUNC({m0})) * 30.436875)"
            s = f"({s0} + ({d0} + ({m0} - TRUNC({m0})) * 30.436875 - {d}) * 86400)"
            return self.shared([m, d, s], written)

        def written(values: list[str]) -> str:
            months, days, seconds = values
            years = f"TRUNC({months} / 12)"
            hours = f"TRUNC({seconds} / 3600)"
            minutes = f"(TRUNC({seconds} / 60) - {hours} * 60)"
            secs = f"({seconds} - TRUNC({seconds} / 60) * 60)"

            def unit(value: str, letter: str) -> str:
                return f"CASE WHEN {value} <> 0 THEN {_number(value)} || '{letter}' ELSE '' END"

            return (
                f"CASE WHEN {months} = 0 AND {days} = 0 AND {seconds} = 0 THEN 'PT0S' WHEN {seconds} IS NOT NULL THEN "
                f"'P' || {unit(years, 'Y')} || {unit(f'({months} - {years} * 12)', 'M')} || {unit(days, 'D')} || "
                f"CASE WHEN {seconds} <> 0 THEN 'T' || {unit(hours, 'H')} || {unit(minutes, 'M')} || "
                f"{unit(secs, 'S')} ELSE '' END END"
            )

        return self.shared([parts.months, parts.days, parts.seconds], spread)

    def sum(self, a: str, b: str, sign: int) -> str:
        """The duration text of the sum of the duration texts, or with `sign` -1 their difference."""

        def summed(values: list[str]) -> str:
            p, q = self._parts(values[0]), self._parts(values[1])
            op = "-" if sign < 0 else "+"
            return self._text(
                Parts(f"({p.months} {op} {q.months})", f"({p.days} {op} {q.days})", f"({p.seconds} {op} {q.seconds})")
            )

        return self.shared([a, b], summed)

    def times(self, duration: str, factor: str) -> str:
        """The duration text of the duration text times the factor, a SQL number."""

        def scaled(values: list[str]) -> str:
            p = self._parts(values[0])
            f = values[1]
            return self._text(Parts(f"({p.months} * {f})", f"({p.days} * {f})", f"({p.seconds} * {f})"))

        return self.shared([duration, factor], scaled)

    def canonical_duration(self, text: str) -> str:
        """The canonical duration text of the duration text: null where it is none."""
        return self.shared(
            [text],
            lambda t: f"CASE WHEN {self.is_kind(t[0], Temporal.DURATION)} THEN {self._text(self._parts(t[0]))} END",
        )

    def between(
        self, from_: str, to: str, kind: Temporal, unit: str | None = None, to_kind: Temporal | None = None
    ) -> str:
        """
        `duration.between`: the duration from the text `from_` of the kind to the text `to` of `to_kind`, in whole
        months as a calendar counts them, then days, then seconds; with a unit `months`, `days` or `seconds`, in that
        alone. Two zoned values are read as the moments they are; where either is local, both are read on their wall
        clocks.
        """
        to_kind = to_kind or kind
        zoned = kind.zoned and to_kind.zoned

        def read(text: str, of: Temporal) -> str:
            return self.sql.utc(text, of) if zoned else self.sql.local(text, of)

        return self._between(read(from_, kind), read(to, to_kind), kind in _TIMES or to_kind in _TIMES, unit)

    def _between(self, from_: str, to: str, times: bool, unit: str | None) -> str:
        def between(values: list[str]) -> str:
            a, b = values
            months = "0" if times else self.sql.months_between(a, b)
            if unit == "months":
                return self._text(Parts(months, "0", "0"))
            if unit == "days":
                return self._text(Parts("0", f"TRUNC({self.sql.seconds_between(a, b)} / 86400)", "0"))
            if unit == "seconds":
                return self._text(Parts("0", "0", self.sql.seconds_between(a, b)))
            return self.shared(
                [months],
                lambda m: self.shared(
                    [self.sql.seconds_between(self.sql.add_months(a, m[0]), b)],
                    lambda rest: self._text(
                        Parts(m[0], f"TRUNC({rest[0]} / 86400)", f"({rest[0]} - TRUNC({rest[0]} / 86400) * 86400)")
                    ),
                ),
            )

        return self.shared([from_, to], between)


def _duration_field(parts: Parts, name: str) -> str | None:
    """A field of a duration of the parts, by Cypher's name for it: None for a name a duration has no field of."""
    m, d, s = parts.months, parts.days, parts.seconds
    fields = {
        "years": f"TRUNC({m} / 12)",
        "quarters": f"TRUNC({m} / 3)",
        "months": m,
        "weeks": f"TRUNC({d} / 7)",
        "days": d,
        "hours": f"TRUNC({s} / 3600)",
        "minutes": f"TRUNC({s} / 60)",
        "seconds": f"TRUNC({s})",
        "milliseconds": f"TRUNC({s} * 1000)",
        "microseconds": f"TRUNC({s} * 1000000)",
        "nanoseconds": f"TRUNC({s} * 1000000000)",
        "quartersOfYear": f"(TRUNC({m} / 3) - TRUNC({m} / 12) * 4)",
        "monthsOfQuarter": f"({m} - TRUNC({m} / 3) * 3)",
        "monthsOfYear": f"({m} - TRUNC({m} / 12) * 12)",
        "daysOfWeek": f"({d} - TRUNC({d} / 7) * 7)",
        "minutesOfHour": f"(TRUNC({s} / 60) - TRUNC({s} / 3600) * 60)",
        "secondsOfMinute": f"(TRUNC({s}) - TRUNC({s} / 60) * 60)",
        "millisecondsOfSecond": f"(TRUNC({s} * 1000) - TRUNC({s}) * 1000)",
        "microsecondsOfSecond": f"(TRUNC({s} * 1000000) - TRUNC({s}) * 1000000)",
        "nanosecondsOfSecond": f"(TRUNC({s} * 1000000000) - TRUNC({s}) * 1000000000)",
    }
    return fields.get(name)


def _number(value: str) -> str:
    """A number as Neo4j writes one in a duration: no zeros after the point, nor the point where there is no
    fraction."""
    return f"RTRIM(to_char({value}, 'FM9999999999990.999999999'), '.')"


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def canonical(text: str, kind: Temporal) -> str | None:
    """
    The canonical text of the kind of the text, worked out as the store's SQL would work it out, for a value the
    statement writes: None where the text is of no temporal kind. Raises ValueError where the text has a temporal
    value's shape and is none (`2024-02-30`).
    """
    if kind is Temporal.DURATION:
        return _canonical_duration(text) if re.fullmatch(Temporal.DURATION.pattern, text) else None
    from_ = next((it for it in INSTANTS if re.fullmatch(it.pattern, text)), None)
    if from_ is None:
        return None
    found = re.search(OFFSET, text) if from_.zoned else None
    written = found.group(0) if found else None
    local = text[: -len(written)] if written else text
    offset = None
    if written is not None:
        offset = re.sub("^([+-][0-9]{2})([0-9]{2})$", r"\1:\2", re.sub("^[+-]00:?00$", "Z", written))
    if from_ is Temporal.DATE:
        clock = datetime.combine(date.fromisoformat(local), time())
    elif from_ in (Temporal.LOCAL_DATE_TIME, Temporal.DATE_TIME):
        clock = datetime.fromisoformat(local)
    else:
        clock = datetime.combine(date(1970, 1, 1), time.fromisoformat(local))
    micros = clock.microsecond
    if micros == 0:
        fraction = ""
    elif micros % 1000 == 0:
        fraction = f".{micros // 1000:03d}"
    else:
        fraction = f".{micros:06d}"
    clock_text = f"{clock.hour:02d}:{clock.minute:02d}:{clock.second:02d}{fraction}"
    zone = (offset or "Z") if kind.zoned else ""
    if kind is Temporal.DATE:
        return clock.date().isoformat()
    if kind in (Temporal.LOCAL_DATE_TIME, Temporal.DATE_TIME):
        return f"{clock.date().isoformat()}T{clock_text}{zone}"
    return f"{clock_text}{zone}"


def _canonical_duration(text: str) -> str:
    """The canonical text of the duration text, as the SQL writes the text of its parts. Raises ValueError where a
    part is no number (`P1.2.3D`)."""
    match = re.fullmatch(Temporal.DURATION.pattern, text)
    assert match is not None
    with localcontext() as context:
        context.prec = 60

        def part(group: int) -> Decimal:
            found = match.group(group)
            if not found:
                return Decimal(0)
            try:
                return Decimal(found)
            except InvalidOperation as error:
                raise NoNumber(found) from error

        def down(value: Decimal) -> Decimal:
            return value.quantize(Decimal(1), rounding=ROUND_DOWN)

        sign = Decimal(-1) if match.group(1) == "-" else Decimal(1)
        m0 = sign * (part(3) * 12 + part(5))
        d0 = sign * (part(7) * 7 + part(9))
        s0 = sign * (part(12) * 3600 + part(14) * 60 + part(16))
        month_days = Decimal("30.436875")
        months = down(m0)
        days_exact = d0 + (m0 - months) * month_days
        days = down(days_exact)
        seconds = s0 + (days_exact - days) * 86400
        if months == 0 and days == 0 and seconds == 0:
            return "PT0S"
        years = down(months / 12)
        hours = down(seconds / 3600)
        minutes = down((seconds - hours * 3600) / 60)
        secs = seconds - hours * 3600 - minutes * 60

        def unit(value: Decimal, letter: str) -> str:
            return f"{format(value.normalize(), 'f')}{letter}" if value != 0 else ""

        clock = f"T{unit(hours, 'H')}{unit(minutes, 'M')}{unit(secs, 'S')}" if seconds != 0 else ""
        return f"P{unit(years, 'Y')}{unit(months - years * 12, 'M')}{unit(days, 'D')}{clock}"
