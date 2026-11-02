"""
A timestamp as compiled Cypher holds one on SQLite, which has no type for it: the text of a wall clock to the
microsecond, `2024-03-15T10:30:00.000000`, every one as long as every other, so two compare and order as their clocks
do, as text. drivine4j's `SqliteTimestamps`, with the calendar `java.time` works out that the functions which read and
write timestamps ask of one: Python's `datetime` holds no year before 1, and Neo4j numbers them as ISO 8601 does,
astronomically, `0000` for 1 BC and `-0044` for 45 BC.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

MAX_YEAR = 999_999_999
NANOS_PER_SECOND = 1_000_000_000
NANOS_PER_DAY = 86_400 * NANOS_PER_SECOND
_MIN_EPOCH_DAY = -365_243_219_162
_MAX_EPOCH_DAY = 365_241_780_471

_MONTHS = [
    "JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER",
    "DECEMBER",
]  # fmt: skip

_DATE = r"(\d{4}|\+\d{5,10}|-\d{4,10})-(\d{2})-(\d{2})"
"""A date as `java.time` reads an ISO one: a year of four digits, a sign before more, and `-` before one below 0."""

_TIME = r"(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{0,9}))?)?"
"""A time of day: hours and minutes, then its seconds and their fraction where it has them."""

_DATE_TIME = re.compile(f"{_DATE}[Tt]{_TIME}", re.ASCII)
_DATE_ONLY = re.compile(_DATE, re.ASCII)
_TIME_ONLY = re.compile(_TIME, re.ASCII)
_OFFSET = re.compile(r"([+-])(\d{2})(?::(\d{2})(?::(\d{2}))?)?", re.ASCII)


class TemporalError(ValueError):
    """
    A value `java.time` refuses: as `DateTimeException`, of a date past the years it holds; and, where it names the
    text it was asked to read, as `DateTimeParseException`.
    """

    def __init__(self, message: str, parsed: str | None = None) -> None:
        super().__init__(message)
        self.parsed = parsed


@dataclass(frozen=True)
class Clock:
    """A wall clock, a date and a time of day of no zone, as `java.time`'s `LocalDateTime` holds one."""

    year: int
    month: int
    day: int
    hour: int = 0
    minute: int = 0
    second: int = 0
    nano: int = 0

    @property
    def epoch_day(self) -> int:
        return epoch_day(self.year, self.month, self.day)

    @property
    def nano_of_day(self) -> int:
        return (self.hour * 3600 + self.minute * 60 + self.second) * NANOS_PER_SECOND + self.nano

    @property
    def epoch_nanos(self) -> int:
        return self.epoch_day * NANOS_PER_DAY + self.nano_of_day

    @property
    def day_of_week(self) -> int:
        """The ISO day of the week, from 1 for Monday to 7."""
        return (self.epoch_day + 3) % 7 + 1

    @property
    def day_of_year(self) -> int:
        return self.epoch_day - epoch_day(self.year, 1, 1) + 1

    @property
    def iso_week(self) -> tuple[int, int]:
        """The year of the ISO week of the date, and the week, from 1: the year and week of its week's Thursday."""
        thursday = of_epoch_day(self.epoch_day + 4 - self.day_of_week)
        return thursday.year, (thursday.day_of_year - 1) // 7 + 1

    def at_midnight(self) -> Clock:
        return Clock(self.year, self.month, self.day)

    def truncated(self, nanos: int) -> Clock:
        """The clock cut to a whole number of `nanos`, a part of a second."""
        return replace(self, nano=self.nano - self.nano % nanos)


def read(text: str | None) -> Clock | None:
    """The timestamp `text`, a space taken for the `T`, or None where it is null."""
    return None if text is None else date_time(text.replace(" ", "T"))


def write(clock: Clock) -> str:
    """The clock as a timestamp's text, to the microsecond, its fraction cut, as `uuuu-MM-dd'T'HH:mm:ss.SSSSSS`."""
    return (
        f"{_year_text(clock.year)}-{clock.month:02d}-{clock.day:02d}T{clock.hour:02d}:{clock.minute:02d}:"
        f"{clock.second:02d}.{clock.nano // 1000:06d}"
    )


def _year_text(year: int) -> str:
    """A year as `uuuu` writes one: four digits at least, a sign before one past 9999, and before one below 0."""
    if year > 9999:
        return f"+{year}"
    return f"-{-year:04d}" if year < 0 else f"{year:04d}"


def date_time(text: str) -> Clock:
    """The ISO date and time, as `LocalDateTime.parse` reads it."""
    found = _DATE_TIME.fullmatch(text)
    if found is None:
        raise _unparsed(text)
    return _clock(text, list(found.groups()))


def date(text: str) -> Clock:
    """The ISO date at its midnight, as `LocalDate.parse(text).atStartOfDay()` reads it."""
    found = _DATE_ONLY.fullmatch(text)
    if found is None:
        raise _unparsed(text)
    return _clock(text, [*found.groups(), "00", "00", None, None])


def time(text: str) -> Clock:
    """The ISO time of day on 1970-01-01, as `LocalTime.parse(text).atDate(EPOCH_DAY)` reads it."""
    found = _TIME_ONLY.fullmatch(text)
    if found is None:
        raise _unparsed(text)
    return _clock(text, ["1970", "01", "01", *found.groups()])


def offset_seconds(offset: str, whole: str) -> int:
    """
    The offset a moment's text ends with, `Z` or `±hh[:mm[:ss]]`, in seconds ahead of UTC, as an `OffsetDateTime`'s is
    read: past ±18:00, or of minutes or seconds past 59, it is refused.
    """
    if offset in ("Z", "z"):
        return 0
    found = _OFFSET.fullmatch(offset)
    if found is None:
        raise _unparsed(whole)
    sign, hours, minutes, seconds = found.groups()
    m, s = int(minutes or 0), int(seconds or 0)
    total = int(hours) * 3600 + m * 60 + s
    if m > 59 or s > 59 or total > 18 * 3600:
        raise TemporalError(
            f"Text '{whole}' could not be parsed: Zone offset not in valid range: -18:00 to +18:00", whole
        )
    return -total if sign == "-" else total


def _unparsed(text: str) -> TemporalError:
    return TemporalError(f"Text '{text}' could not be parsed at index 0", text)


def _clock(text: str, fields: list[str | None]) -> Clock:
    """The clock of the fields the text was read into, each checked as `java.time` checks it."""
    year, month, day, hour, minute, second, fraction = fields
    value = Clock(
        int(year or 0),
        int(month or 0),
        int(day or 0),
        int(hour or 0),
        int(minute or 0),
        int(second or 0),
        int((fraction or "").ljust(9, "0")),
    )

    def check(name: str, low: int, high: int, of: int) -> None:
        if not low <= of <= high:
            raise TemporalError(
                f"Text '{text}' could not be parsed: Invalid value for {name} (valid values {low} - {high}): {of}", text
            )

    check("Year", -MAX_YEAR, MAX_YEAR, value.year)
    check("MonthOfYear", 1, 12, value.month)
    check("DayOfMonth", 1, 31, value.day)
    check("HourOfDay", 0, 23, value.hour)
    check("MinuteOfHour", 0, 59, value.minute)
    check("SecondOfMinute", 0, 59, value.second)
    if value.day > length_of_month(value.year, value.month):
        because = (
            f"Invalid date 'February 29' as '{value.year}' is not a leap year"
            if value.day == 29
            else f"Invalid date '{_MONTHS[value.month - 1]} {value.day}'"
        )
        raise TemporalError(f"Text '{text}' could not be parsed: {because}", text)
    return value


def is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def length_of_month(year: int, month: int) -> int:
    if month == 2:
        return 29 if is_leap(year) else 28
    return 30 if month in (4, 6, 9, 11) else 31


def epoch_day(year: int, month: int, day: int) -> int:
    """The days from 1970-01-01 to the date of the proleptic Gregorian calendar, as `LocalDate.toEpochDay` counts."""
    y = year - (1 if month <= 2 else 0)
    era = y // 400
    of_era = y - era * 400
    of_year = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    of_era_day = of_era * 365 + of_era // 4 - of_era // 100 + of_year
    return era * 146_097 + of_era_day - 719_468


def of_epoch_day(days: int) -> Clock:
    """The date `days` from 1970-01-01, at its midnight, refused past the years `java.time` holds."""
    if not _MIN_EPOCH_DAY <= days <= _MAX_EPOCH_DAY:
        raise TemporalError(f"Invalid value for EpochDay (valid values {_MIN_EPOCH_DAY} - {_MAX_EPOCH_DAY}): {days}")
    z = days + 719_468
    era = z // 146_097
    of_era = z - era * 146_097
    year_of_era = (of_era - of_era // 1460 + of_era // 36_524 - of_era // 146_096) // 365
    of_year = of_era - (365 * year_of_era + year_of_era // 4 - year_of_era // 100)
    shifted = (5 * of_year + 2) // 153
    day = of_year - (153 * shifted + 2) // 5 + 1
    month = shifted + (3 if shifted < 10 else -9)
    year = year_of_era + era * 400 + (1 if month <= 2 else 0)
    return Clock(year, month, day)


def of_epoch_nanos(nanos: int) -> Clock:
    """The clock `nanos` nanoseconds after 1970-01-01T00:00."""
    days, rest = divmod(nanos, NANOS_PER_DAY)
    date = of_epoch_day(days)
    seconds, nano = divmod(rest, NANOS_PER_SECOND)
    return Clock(date.year, date.month, date.day, seconds // 3600, seconds // 60 % 60, seconds % 60, nano)
