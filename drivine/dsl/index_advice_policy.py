from enum import StrEnum


class IndexAdvicePolicy(StrEnum):
    """
    What to do when an ordered query has no range index to seek into. Ordering without one is correct: it scans and
    sorts, which is fine for a few nodes and quietly expensive for many. So this is advice, at the level the caller
    picks.
    """

    OFF = "OFF"
    """Say nothing."""

    WARN = "WARN"
    """The default. Log once for each label and properties."""

    FAIL = "FAIL"
    """Raise, for development and CI."""
