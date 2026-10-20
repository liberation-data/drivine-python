# Written by scripts/unasync.py from drivine/store/store_identity_resolver.py:
# change that, and run the script again.
from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from drivine.drivine_error import DrivineError
from drivine.schema.schema_specs import uniqueness_constraint
from drivine.store.store_identity import StoreIdentity
from drivine.sync import concurrency

if TYPE_CHECKING:
    from drivine.backend.graph_backend import GraphBackend
    from drivine.sync.manager.persistence_manager import PersistenceManager
    from drivine.sync.schema.constraint_manager import ConstraintManager

logger = logging.getLogger("drivine.store")

LABEL = "DrivineStore"
"""The stamp's node, as drivine4j and TypeScript write it, so a store stamped from any of them has one identity."""
SINGLETON = "singleton"
_KEY_CONSTRAINT = uniqueness_constraint(LABEL, "key")
_ATTEMPTS = 3
_MERGE_ATTEMPTS = 5
_MERGE_BACKOFF_SECONDS = 0.05
_LOST_RACE = re.compile(r"constraint|already exists|conflict|another write", re.IGNORECASE)


class StoreIdentityResolver:
    """
    Reads a store's identity, minting it on first sight, from a single `(:DrivineStore {key: 'singleton'})` node.

    Uniqueness is enforced where the engine allows it: a uniqueness constraint on the key is ensured first, so of two
    processes that boot against a new store at once, the loser's MERGE fails, is retried, and matches the winner's.
    Where duplicate stamps already exist the oldest is used, tie-broken by id; they are logged, never deleted.
    """

    def __init__(
        self, persistence_manager: PersistenceManager, backend: GraphBackend, constraints: ConstraintManager | None
    ) -> None:
        self.persistence_manager = persistence_manager
        self.backend = backend
        self.constraints = constraints

    def resolve(self) -> StoreIdentity:
        guarded = self._ensure_unique_key()
        database = self.persistence_manager.database
        for _ in range(_ATTEMPTS):
            self._assign_if_absent(guarded)
            stamps = self.persistence_manager.query(self.backend.store_stamps().spec())
            if stamps:
                if len(stamps) > 1:
                    logger.warning(
                        "%s DrivineStore stamps on %s: concurrent first boots each minted one. Using the oldest, %s.",
                        len(stamps),
                        database,
                        stamps[0]["id"],
                    )
                return StoreIdentity(stamps[0]["id"], self.persistence_manager.type, stamps[0]["assignedAt"])
            logger.warning("The DrivineStore stamp on %s vanished between write and read; again.", database)
        raise DrivineError(
            f"Could not establish the identity of the store at {database}: a stamp was written {_ATTEMPTS} times and "
            f"never read back."
        )

    def _ensure_unique_key(self) -> bool:
        if self.constraints is None:
            return False
        try:
            result = self.constraints.ensure(_KEY_CONSTRAINT)
        except Exception as error:
            logger.warning("Could not ensure the DrivineStore key constraint: %s. Identity is best-effort.", error)
            return False
        if result.status == "VIOLATION":
            logger.warning(
                "%s already holds duplicate DrivineStore stamps, so their uniqueness can't be enforced. Identity is "
                "best-effort until they are removed.",
                self.persistence_manager.database,
            )
            return False
        return True

    def _assign_if_absent(self, guarded: bool) -> None:
        attempt = 1
        while True:
            try:
                self.persistence_manager.execute(
                    self.backend.assign_store_stamp().spec(
                        {
                            "key": SINGLETON,
                            "storeId": str(uuid.uuid4()),
                            "assignedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                            "engine": self.persistence_manager.type.value,
                        }
                    )
                )
                return
            except Exception as error:
                # Lost the race to a peer's first boot: its stamp is there, or about to be. Back off and merge onto it.
                if not guarded or attempt >= _MERGE_ATTEMPTS or not _LOST_RACE.search(str(error)):
                    raise
                concurrency.sleep(_MERGE_BACKOFF_SECONDS * 2 ** (attempt - 1))
                attempt += 1
