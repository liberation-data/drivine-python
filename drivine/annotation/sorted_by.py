from dataclasses import dataclass


@dataclass(frozen=True)
class SortedBy:
    """
    Sorts a list relationship of a view after it is loaded, by a field path: `"name"`, or `"person.name"` into a
    related view or relationship fragment. Nulls go last.
    """

    path: str
    ascending: bool = True
