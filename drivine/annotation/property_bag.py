from dataclasses import dataclass


@dataclass(frozen=True)
class PropertyBag:
    """
    Maps a dict field of a node fragment to node properties, one for each entry, stored as `<prefix><delimiter><key>`:
    real properties, visible and indexable. `prefix` defaults to the field's stored name and `delimiter` to `.`.

    A flat bag has no prefix: each entry is stored under its bare key, and the bag reads back every property no field
    or other bag accounts for. One to a fragment.
    """

    prefix: str | None = None
    delimiter: str = "."
    flat: bool = False
