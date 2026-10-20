from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime, time
from enum import Enum
from typing import Any

from drivine.drivine_error import DrivineError

_PLAIN_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class FalkorDbParameterCoercer:
    """
    Prepares parameters for FalkorDB, whose client writes them into the statement as text (`CYPHER name=value ...`)
    rather than sending them apart from it, as TypeScript's Drivine prepares them.

    - A datetime is written as ISO 8601 in UTC with all nine digits of its second (`2040-01-01T00:00:00.000000000Z`).
      FalkorDB holds it as text and compares it as text, which sorts as time only when every value is the same width.
      Nine digits matches drivine4j and TypeScript, so a store written from any of them compares alike. A naive
      datetime is taken as UTC. A date or time is written as its ISO text.
    - A set or tuple is written as a list; Cypher has no set. An Enum is written as its value.
    - A parameter name that isn't a plain name is refused: the client writes names unquoted.
    - A number that isn't finite is refused, as Cypher has no literal for it.

    Lists and maps are coerced all the way down.
    """

    def coerce(self, parameters: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, value in parameters.items():
            if not _PLAIN_NAME.match(name):
                raise DrivineError(
                    f"FalkorDB can't take the parameter name {name!r}: it must be letters, digits and underscores, "
                    f"not starting with a digit."
                )
            result[name] = self._value(value)
        return result

    def _value(self, value: Any) -> Any:
        if value is None or isinstance(value, (str, bool)):
            return value
        if isinstance(value, Enum):
            return self._value(value.value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise DrivineError(f"FalkorDB can't take {value} as a parameter value.")
            return value
        if isinstance(value, datetime):
            return to_fixed_width_iso(value)
        if isinstance(value, (date, time)):
            return value.isoformat()
        if isinstance(value, (list, tuple, set, frozenset)):
            return [self._value(it) for it in value]  # pyright: ignore[reportUnknownVariableType]
        if isinstance(value, Mapping):
            return {str(key): self._value(item) for key, item in value.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        raise DrivineError(f"FalkorDB can't take a {type(value).__name__} as a parameter value.")


def to_fixed_width_iso(moment: datetime) -> str:
    """A moment as ISO 8601 in UTC with nine digits of the second, as drivine4j and TypeScript write it."""
    utc = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
    return f"{utc.strftime('%Y-%m-%dT%H:%M:%S')}.{utc.microsecond:06d}000Z"
