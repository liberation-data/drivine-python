from dataclasses import dataclass


@dataclass(frozen=True)
class ReadOnly:
    """
    Marks a relationship field of a view that is loaded and never written. A list of fragments read over several hops
    (`max_depth` above 1) is read-only whether or not it is marked so.
    """
