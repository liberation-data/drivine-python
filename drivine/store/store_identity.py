from __future__ import annotations

from dataclasses import dataclass

from drivine.connection.database_type import DatabaseType


@dataclass(frozen=True)
class StoreIdentity:
    """
    The stable identity of the store behind a connection: who the graph is, not where it lives. A recreated container
    answers at the old address with no data, and a restored backup is the same data at a new one. The id is minted
    once, on first sight of a store, and lives in the store, so two connections report the same id when, and only
    when, they look at the same data.
    """

    id: str
    """Assigned the first time Drivine saw the store. Never reassigned."""
    engine: DatabaseType
    """The engine connected now."""
    assigned_at: str
    """When the id was minted, as ISO 8601."""
