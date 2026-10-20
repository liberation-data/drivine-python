from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PropertyBagModel:
    field: str
    namespace: str
    """What every entry's property name starts with: `<prefix><delimiter>`, or '' for a flat bag."""
    flat: bool
