from dataclasses import dataclass


@dataclass(frozen=True)
class Transient:
    """
    Marks a field that is neither loaded nor saved. It needs a default.
    """
