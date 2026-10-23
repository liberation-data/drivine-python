from __future__ import annotations

from typing import Any

from drivine.drivine_error import DrivineError


class StaleObjectError(DrivineError):
    """
    A save was refused because the node is no longer as the object's stamp says: another writer changed or deleted it
    since the object was loaded. Nothing was written. Load the object again and re-apply the change.
    """

    def __init__(self, type_: type, id: Any, expected_stamp: str, found_stamp: str | None, deleted: bool) -> None:
        if deleted:
            message = f"{type_.__name__} {id!r} was deleted since it was loaded."
        elif found_stamp is None:
            message = (
                f"{type_.__name__} {id!r} may have changed since it was loaded: another writer held it each time the "
                f"save was tried."
            )
        elif found_stamp == expected_stamp:
            message = (
                f"{type_.__name__} {id!r} changed since it was loaded, under the stamp {found_stamp}: a field is not "
                f"as it was loaded."
            )
        else:
            message = (
                f"{type_.__name__} {id!r} changed since it was loaded: expected stamp {expected_stamp}, found "
                f"{found_stamp}."
            )
        super().__init__(message)
        self.type = type_
        self.id = id
        self.expected_stamp = expected_stamp
        self.found_stamp = found_stamp
        """What the node carries now; None when it is gone, or when the engine turned the save away each time."""
        self.deleted = deleted
