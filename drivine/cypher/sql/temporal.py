from __future__ import annotations

from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Any


class Temporal(Enum):
    """
    A kind of temporal value, as openCypher has them. On a SQL store a temporal value is the ISO 8601 text a store keeps
    a moment as, and `pattern` tells the kind from the text: a regular expression both stores read, written with
    capturing groups alone, for Oracle's take no other kind.
    """

    DATE = ("^[0-9]{4}-[0-9]{2}-[0-9]{2}$", False)
    LOCAL_TIME = ("^[0-9]{2}:[0-9]{2}(:[0-9]{2}([.][0-9]+)?)?$", False)
    TIME = ("^[0-9]{2}:[0-9]{2}(:[0-9]{2}([.][0-9]+)?)?(Z|[+-][0-9]{2}:?[0-9]{2})$", True)
    LOCAL_DATE_TIME = ("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(:[0-9]{2}([.][0-9]+)?)?$", False)
    DATE_TIME = (
        "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(:[0-9]{2}([.][0-9]+)?)?(Z|[+-][0-9]{2}:?[0-9]{2})$",
        True,
    )
    DURATION = (
        # `P1Y2M3W4DT5H6M7.5S`, each part optional and signed, or the whole negated: its parts are read by
        # DURATION_PARTS.
        "^(-?)P((-?[0-9.]+)Y)?((-?[0-9.]+)M)?((-?[0-9.]+)W)?((-?[0-9.]+)D)?(T((-?[0-9.]+)H)?((-?[0-9.]+)M)?((-?[0-9.]+)S)?)?$",
        False,
    )

    def __init__(self, pattern: str, zoned: bool) -> None:
        self.pattern = pattern
        self.zoned = zoned

    @property
    def function(self) -> str:
        """The name of the function that makes a value of this kind: `date`, `localdatetime`…"""
        return self.name.lower().replace("_", "")

    @property
    def instant(self) -> bool:
        """Whether a value of this kind is a moment or a time of day, which a duration is added to."""
        return self is not Temporal.DURATION

    @staticmethod
    def of_function(name: str) -> Temporal | None:
        """The kind of a function that makes a temporal value, by its name in lower case: `date`, `datetime`…"""
        return _FUNCTIONS.get(name)

    @staticmethod
    def of_value(value: Any) -> Temporal | None:
        """The kind a parameter bound to the value is, as Drivine writes such a value: None for any other."""
        # A datetime is a date: it is told apart first.
        if isinstance(value, datetime):
            return Temporal.LOCAL_DATE_TIME if value.tzinfo is None else Temporal.DATE_TIME
        if isinstance(value, date):
            return Temporal.DATE
        if isinstance(value, time):
            return Temporal.LOCAL_TIME if value.tzinfo is None else Temporal.TIME
        if isinstance(value, timedelta):
            return Temporal.DURATION
        return None


INSTANTS = (Temporal.DATE_TIME, Temporal.LOCAL_DATE_TIME, Temporal.DATE, Temporal.TIME, Temporal.LOCAL_TIME)
"""The kinds of a moment or a time of day, the zoned before the local of each, as a text is told apart."""

DURATION_DAYS = "^(-?)P((-?[0-9.]+)Y)?((-?[0-9.]+)M)?((-?[0-9.]+)W)?((-?[0-9.]+)D)?"
"""
The days of a duration, from its start: its sign, then years, months, weeks and days, in groups DURATION_PARTS names,
the sign aside. A duration is read in two patterns, its days and its time, for Oracle reads no group past the ninth.
"""

DURATION_TIME = "T((-?[0-9.]+)H)?((-?[0-9.]+)M)?((-?[0-9.]+)S)?$"
"""The time of a duration, to its end: hours, minutes and seconds, in groups DURATION_PARTS names."""

DURATION_PARTS = {
    "years": (DURATION_DAYS, 3),
    "months": (DURATION_DAYS, 5),
    "weeks": (DURATION_DAYS, 7),
    "days": (DURATION_DAYS, 9),
    "hours": (DURATION_TIME, 2),
    "minutes": (DURATION_TIME, 4),
    "seconds": (DURATION_TIME, 6),
}
"""The pattern, DURATION_DAYS or DURATION_TIME, and the group of it, that holds each part of a duration."""

OFFSET = "(Z|[+-][0-9]{2}:?[0-9]{2})$"
"""The offset a zoned text ends with."""

_FUNCTIONS = {
    "date": Temporal.DATE,
    "localtime": Temporal.LOCAL_TIME,
    "time": Temporal.TIME,
    "localdatetime": Temporal.LOCAL_DATE_TIME,
    "datetime": Temporal.DATE_TIME,
    "duration": Temporal.DURATION,
}
