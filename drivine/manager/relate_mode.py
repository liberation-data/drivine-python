from enum import StrEnum


class RelateMode(StrEnum):
    MERGE = "MERGE"
    """A relationship of the type is made only when there is none; its properties are set whether made or found."""
    CREATE = "CREATE"
    """Each call makes another."""
