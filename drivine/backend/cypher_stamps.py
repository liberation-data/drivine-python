"""Stamps as a save statement writes them. See drivine.model.stamps."""

from __future__ import annotations

from drivine.model import stamps

STAMP = "`__drivine.stamp`"
"""The stamp property, quoted: its name holds a dot."""
LOCK = "`__drivine.lock`"
"""Set and removed within a checked save, to take the node's write lock before its stamp is compared."""
MADE = "`__drivine.made`"
"""Set and removed within a save, to tell a node the save made from one it found."""
OFFERED = "$_stamp"
"""The parameter holding the stamp a save offers the nodes it marks."""
EXPECTED = "$_expected"
"""The parameter holding the stamp a checked save expects the root to carry."""

_T = stamps.TOKEN


def changes(alias: str, props: str) -> str:
    """
    Whether writing `props` (a map expression) would change a property of the entity `alias`. A null in `props` clears
    a property, and changes it only if it is there.
    """
    return (
        f"any(k IN keys({props}) WHERE CASE WHEN {props}[k] IS NULL THEN {alias}[k] IS NOT NULL "
        f"ELSE {alias}[k] IS NULL OR {alias}[k] <> {props}[k] END)"
    )


def restamp(alias: str, condition: str) -> str:
    """Replaces the node's data token when `condition` holds, or gives it a whole stamp when it has none."""
    two = stamps.two_tokens_of(alias)
    return (
        f"{alias}.{STAMP} = CASE WHEN ({condition}) OR {two} IS NULL "
        f"THEN left({OFFERED}, {_T}) + coalesce(right({two}, {_T + 1}), right({OFFERED}, {_T + 1})) "
        f"ELSE {alias}.{STAMP} END"
    )


def relink(alias: str, condition: str = "true") -> str:
    """Replaces the node's links token when `condition` holds, or gives it a whole stamp when it has none."""
    two = stamps.two_tokens_of(alias)
    return (
        f"{alias}.{STAMP} = CASE WHEN ({condition}) OR {two} IS NULL "
        f"THEN coalesce(left({two}, {_T + 1}), left({OFFERED}, {_T + 1})) + right({OFFERED}, {_T}) "
        f"ELSE {alias}.{STAMP} END"
    )
