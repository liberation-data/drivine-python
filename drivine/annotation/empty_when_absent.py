from dataclasses import dataclass


@dataclass(frozen=True)
class EmptyWhenAbsent:
    """
    Loads an absent or null collection as an empty one: a list, set or tuple as one with no items, a dict as `{}`.
    """
