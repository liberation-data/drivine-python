from dataclasses import dataclass


@dataclass(frozen=True)
class JsonPacked:
    """
    Stores a field as a JSON string, for a value an engine can't store as a property: a list of maps, or a map.
    Unpacked on load.
    """
