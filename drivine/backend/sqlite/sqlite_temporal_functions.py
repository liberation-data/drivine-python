"""
The functions compiled Cypher calls on SQLite to work out temporal values, which SQLite has no type for: drivine4j's
`SqliteTemporalFunction` and `SqliteToCharFunction`, over the timestamps of sqlite_timestamps.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any

from drivine.backend.sqlite import sqlite_arguments as arguments
from drivine.backend.sqlite import sqlite_timestamps as timestamps
from drivine.backend.sqlite.sqlite_timestamps import NANOS_PER_SECOND, Clock, TemporalError
from drivine.drivine_error import DrivineError

_OFFSET = re.compile(r"(Z|[+-][0-9]{2}:?[0-9]{2})$")
"""The offset a zoned text ends with, as drivine4j's `Temporal.OFFSET` finds it."""

_KINDS = ("DATE", "LOCAL_TIME", "TIME", "LOCAL_DATE_TIME", "DATE_TIME", "DURATION")

_YEARS_OF = {"millennium": 1000, "century": 100, "decade": 10}

_DECIMAL = re.compile(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")
"""A number as Java's `new BigDecimal(text)` reads one."""


def temporal(*values: Any) -> Any:
    """
    `drivine_temporal(operation, …)`: what compiled Cypher asks of a timestamp on SQLite, worked out as `java.time`
    works it out, as `SqliteTemporalSql` names each operation. Null where an argument is.

    - `local(text, kind)`: the wall clock the ISO text of a temporal kind reads, its offset left out: a date at its
      midnight, a time of day on 1970-01-01.
    - `utc(text, kind)`: the moment the text is, in UTC; for a kind of no offset, its wall clock.
    - `extract(timestamp, field)`: `YEAR`, `MONTH`, `DAY`, `HOUR` or `MINUTE` as a whole number, `SECOND` with its
      fraction.
    - `day_of_week(timestamp)`: the ISO day of the week, from 1 for Monday to 7.
    - `add_months(timestamp, months)`: so many months later, a day past the month's end its last day.
    - `add_time(timestamp, days, seconds)`: so many days and seconds later.
    - `months_between(from, to)`: the whole months a calendar counts from one to the other, negative where `to` is
      before.
    - `seconds_between(from, to)`: the seconds from one to the other, with their fraction.
    - `truncate(timestamp, unit)`: cut to the `millennium`, `century`, `decade`, `year`, ISO `weekYear`, `quarter`,
      `month`, ISO `week`, `day`, `hour`, `minute`, `second`, `millisecond` or `microsecond`, as Neo4j cuts.

    A text of the kind's shape that names no such value, a 30th of February, fails the statement, as it does on
    Postgres.
    """
    operation = arguments.text(values[0]) if values else None
    if any(it is None for it in values[1:]):
        return None
    run = _OPERATIONS.get(operation or "")
    if run is None:
        raise DrivineError(f"drivine_temporal has no operation {operation}.")
    try:
        return run(values)
    except TemporalError as error:
        if error.parsed is not None:
            raise DrivineError(f"'{error.parsed}' is no temporal value: {error}") from error
        raise DrivineError(str(error)) from error


def _text(values: tuple[Any, ...], i: int) -> str:
    return arguments.text(values[i]) or ""


def _clock(values: tuple[Any, ...], i: int) -> Clock:
    clock = timestamps.read(_text(values, i))
    assert clock is not None
    return clock


def _whole(value: Any) -> int:
    """The argument as a whole number, rounded half away from zero, as Postgres casts a number to one."""
    if arguments.is_integer(value):
        return int(value)
    written = arguments.text(value) or ""
    if not _DECIMAL.fullmatch(written):
        raise DrivineError(f"Character array is missing \"exponent\" mark 'e' or 'E': {written} is no number.")
    with localcontext() as context:
        context.prec = 1000
        return int(Decimal(written).quantize(Decimal(1), ROUND_HALF_UP))


def _kind(name: str) -> str:
    if name not in _KINDS:
        raise DrivineError(f"No enum constant org.drivine.cypher.sql.Temporal.{name}")
    return name


def _unzoned(text: str) -> str:
    return _OFFSET.sub("", text, count=1)


def _with_colon(text: str) -> str:
    def written(found: re.Match[str]) -> str:
        offset = found.group(0)
        return offset if offset == "Z" or ":" in offset else f"{offset[:3]}:{offset[3:]}"

    return _OFFSET.sub(written, text, count=1)


def _micros(clock: Clock) -> Clock:
    return clock.truncated(1000)


def _local(text: str, kind: str) -> Clock:
    if kind == "DATE":
        return timestamps.date(text)
    if kind == "LOCAL_DATE_TIME":
        return _micros(timestamps.date_time(text))
    if kind == "DATE_TIME":
        return _micros(timestamps.date_time(_unzoned(text)))
    if kind == "LOCAL_TIME":
        return _micros(timestamps.time(text))
    if kind == "TIME":
        return _micros(timestamps.time(_unzoned(text)))
    raise DrivineError("A duration is no moment.")


_ZONE = re.compile(r"(Z|[+-]\d{2}(?::\d{2}(?::\d{2})?)?)$", re.IGNORECASE)
"""The offset `OffsetDateTime.parse` reads at a moment's end: `Z`, in either case, or hours, minutes and seconds."""


def _utc(text: str, kind: str) -> Clock:
    if kind not in ("DATE_TIME", "TIME"):
        return _local(text, kind)
    written = _with_colon(text)
    whole = f"1970-01-01T{written}" if kind == "TIME" else written
    zone = _ZONE.search(whole)
    if zone is None:
        raise TemporalError(f"Text '{whole}' could not be parsed at index {len(whole)}", whole)
    clock = timestamps.date_time(whole[: zone.start()])
    seconds = timestamps.offset_seconds(zone.group(0), whole)
    return _micros(timestamps.of_epoch_nanos(clock.epoch_nanos - seconds * NANOS_PER_SECOND))


def _extract(clock: Clock, field: str) -> int | float:
    if field == "SECOND":
        return clock.second + clock.nano / 1e9
    parts = {"YEAR": clock.year, "MONTH": clock.month, "DAY": clock.day, "HOUR": clock.hour, "MINUTE": clock.minute}
    if field not in parts:
        raise DrivineError(f"A timestamp has no field {field}.")
    return parts[field]


def _plus_months(clock: Clock, months: int) -> Clock:
    year, month = divmod(clock.year * 12 + clock.month - 1 + months, 12)
    if not -timestamps.MAX_YEAR <= year <= timestamps.MAX_YEAR:
        raise TemporalError(f"Invalid value for Year (valid values -999999999 - 999999999): {year}")
    return replace(clock, year=year, month=month + 1, day=min(clock.day, timestamps.length_of_month(year, month + 1)))


def _plus_days(clock: Clock, days: int) -> Clock:
    date = timestamps.of_epoch_day(clock.epoch_day + days)
    return replace(clock, year=date.year, month=date.month, day=date.day)


def _add_time(values: tuple[Any, ...]) -> str:
    moved = _plus_days(_clock(values, 1), _whole(values[2]))
    micros = arguments.java_round(arguments.real(values[3]) * 1_000_000)
    return timestamps.write(timestamps.of_epoch_nanos(moved.epoch_nanos + micros * 1000))


def _months_between(start: Clock, end: Clock) -> int:
    """`ChronoUnit.MONTHS.between`: a time of day not yet come round on the last day does not count it."""
    to_day = end.epoch_day
    if to_day > start.epoch_day and end.nano_of_day < start.nano_of_day:
        to_day -= 1
    elif to_day < start.epoch_day and end.nano_of_day > start.nano_of_day:
        to_day += 1
    last = timestamps.of_epoch_day(to_day)

    def packed(clock: Clock) -> int:
        return (clock.year * 12 + clock.month - 1) * 32 + clock.day

    difference = packed(last) - packed(start)
    # Toward zero, as Java divides.
    return difference // 32 if difference >= 0 else -(-difference // 32)


def _seconds_between(start: Clock, end: Clock) -> float:
    seconds, nano = divmod(end.epoch_nanos - start.epoch_nanos, NANOS_PER_SECOND)
    return float(seconds) + nano / 1e9


def _truncate(clock: Clock, unit: str) -> Clock:
    if unit == "year":
        return Clock(clock.year, 1, 1)
    if unit == "quarter":
        return Clock(clock.year, (clock.month - 1) // 3 * 3 + 1, 1)
    if unit == "month":
        return Clock(clock.year, clock.month, 1)
    if unit == "week":
        return _plus_days(clock.at_midnight(), 1 - clock.day_of_week)
    if unit == "day":
        return clock.at_midnight()
    if unit == "hour":
        return replace(clock, minute=0, second=0, nano=0)
    if unit == "minute":
        return replace(clock, second=0, nano=0)
    if unit == "second":
        return replace(clock, nano=0)
    if unit == "millisecond":
        return clock.truncated(1_000_000)
    if unit == "microsecond":
        return clock.truncated(1000)
    if unit in _YEARS_OF:
        # The year less its years into the span, counted toward zero, as Neo4j counts: -1234's millennium is -1000.
        years = _YEARS_OF[unit]
        into = abs(clock.year) % years
        return Clock(clock.year - (into if clock.year >= 0 else -into), 1, 1)
    if unit == "weekYear":
        week = clock.iso_week[1]
        return _plus_days(clock.at_midnight(), -((week - 1) * 7 + clock.day_of_week - 1))
    raise DrivineError(f"A timestamp is not truncated to {unit}.")


_OPERATIONS: dict[str, Callable[[tuple[Any, ...]], Any]] = {
    "local": lambda v: timestamps.write(_local(_text(v, 1), _kind(_text(v, 2)))),
    "utc": lambda v: timestamps.write(_utc(_text(v, 1), _kind(_text(v, 2)))),
    "extract": lambda v: _extract(_clock(v, 1), _text(v, 2)),
    "day_of_week": lambda v: _clock(v, 1).day_of_week,
    "add_months": lambda v: timestamps.write(_plus_months(_clock(v, 1), _whole(v[2]))),
    "add_time": _add_time,
    "months_between": lambda v: _months_between(_clock(v, 1), _clock(v, 2)),
    "seconds_between": lambda v: _seconds_between(_clock(v, 1), _clock(v, 2)),
    "truncate": lambda v: timestamps.write(_truncate(_clock(v, 1), _text(v, 2))),
}

_TOKENS = ("HH24", "IYYY", "YYYY", "DDD", "IW", "MM", "DD", "MI", "SS")
"""The parts of a timestamp's pattern, the longer of two that start alike first."""


def to_char(value: Any, pattern: Any) -> str | None:
    """
    `drivine_to_char(value, pattern)`: a timestamp or a number written in a pattern of Postgres's and Oracle's
    `to_char`, those compiled Cypher writes temporal values with. Null where the value or the pattern is.

    - A timestamp's pattern is made of `YYYY`, `MM`, `DD`, `HH24`, `MI`, `SS`, `IYYY` (the ISO week's year), `IW` (its
      week) and `DDD` (the day of the year), and text in double quotes, written as it is; anything else is itself. A
      year is written with its sign where it is before 0: `0000`, `-0044`.
    - A number's opens with `FM`: `0`s for its digits, a whole number padded to as many, and after a point as many
      `9`s as the fraction may have digits, rounded half up, its trailing zeros left out (`2.5`, `2.`).
    """
    written = arguments.text(pattern)
    if written is None or value is None:
        return None
    if written.startswith("FM"):
        return _number_text(value, written[2:])
    try:
        return _clock_text(value, written)
    except TemporalError as error:
        raise DrivineError(str(error)) from error


def _number_text(value: Any, pattern: str) -> str:
    if arguments.is_integer(value):
        number = Decimal(int(value))
    else:
        written = arguments.text(value) or ""
        if not _DECIMAL.fullmatch(written):
            raise DrivineError(f"{written} is no number to write in a pattern.")
        number = Decimal(written)
    with localcontext() as context:
        context.prec = 100_000
        point = pattern.find(".")
        if point < 0:
            whole = number.quantize(Decimal(1), ROUND_HALF_UP)
            digits = format(abs(whole), "f").rjust(pattern.count("0"), "0")
            return f"-{digits}" if whole < 0 else digits
        rounded = number.quantize(Decimal(1).scaleb(-(len(pattern) - point - 1)), ROUND_HALF_UP)
        if rounded == 0:
            return "0."
        rounded = rounded.normalize()
        exponent = rounded.as_tuple().exponent
        assert isinstance(exponent, int)
        return f"{rounded:f}." if exponent >= 0 else f"{rounded:f}"


def _clock_text(value: Any, pattern: str) -> str:
    clock = timestamps.read(arguments.text(value))
    assert clock is not None
    written: list[str] = []
    at = 0
    while at < len(pattern):
        if pattern[at] == '"':
            close = pattern.find('"', at + 1)
            end = close if close > at else len(pattern)
            written.append(pattern[at + 1 : end])
            at = end + 1
            continue
        token = next((it for it in _TOKENS if pattern.startswith(it, at)), None)
        if token is None:
            written.append(pattern[at])
            at += 1
            continue
        written.append(_field(clock, token))
        at += len(token)
    return "".join(written)


def _field(clock: Clock, token: str) -> str:
    if token == "HH24":
        return f"{clock.hour:02d}"
    if token == "IYYY":
        return _year(clock.iso_week[0])
    if token == "YYYY":
        return _year(clock.year)
    if token == "DDD":
        return f"{clock.day_of_year:03d}"
    if token == "IW":
        return f"{clock.iso_week[1]:02d}"
    parts = {"MM": clock.month, "DD": clock.day, "MI": clock.minute, "SS": clock.second}
    return f"{parts[token]:02d}"


def _year(year: int) -> str:
    """A year as ISO 8601 writes it, four digits after its sign: `0000` for 1 BC, `-0044` for 45 BC."""
    return f"{'-' if year < 0 else ''}{abs(year):04d}"
