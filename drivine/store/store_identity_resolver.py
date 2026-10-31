from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from drivine import concurrency
from drivine.drivine_error import DrivineError
from drivine.manager.contention import any_cause, contended_pause
from drivine.schema.schema_specs import uniqueness_constraint
from drivine.store.store_identity import StoreIdentity

if TYPE_CHECKING:
    from drivine.backend.graph_backend import GraphBackend
    from drivine.manager.persistence_manager import PersistenceManager
    from drivine.schema.constraint_manager import ConstraintManager
    from drivine.schema.ensure_result import EnsureResult
    from drivine.schema.uniqueness_constraint_spec import UniquenessConstraintSpec

logger = logging.getLogger("drivine.store")

LABEL = "DrivineStore"
"""The stamp's node, as drivine4j and TypeScript write it, so a store stamped from any of them has one identity."""
SINGLETON = "singleton"
_KEY_CONSTRAINT = uniqueness_constraint(LABEL, "key")
_ATTEMPTS = 3
_MERGE_ATTEMPTS = 5
_MERGE_BACKOFF_SECONDS = 0.05
_ENSURE_ATTEMPTS = 3
_VIOLATED_THE_KEY = re.compile(r"unique constraint|already exists", re.IGNORECASE)
_WRITE_CONFLICT = re.compile(r"conflict|another write", re.IGNORECASE)
_MADE_MEANWHILE = re.compile(r"already (exists|indexed)|ORA-00955\b", re.IGNORECASE)
_UNIQUE_VIOLATION = "23505"


def _made_meanwhile(failure: BaseException) -> bool:
    """
    Whether a schema statement was refused for what another session made meanwhile: a constraint or index that already
    exists (FalkorDB's "Constraint already exists", or its "Attribute 'key' is already indexed" for the index the
    constraint needs), or the index's name already taken: on Oracle ORA-00955, and on Postgres a unique violation in
    its catalog, `pg_class` (23505 on a `pg_` index), as two sessions running `CREATE ... IF NOT EXISTS` at once get.
    Not one in a table of the user's, which nothing made meanwhile: a Postgres failure is judged by its SQLSTATE and
    constraint alone, as psycopg's message carries the DETAIL, "Key (...)=(...) already exists.", of any violation. The
    failure and each cause under it are looked at. A refusal for something else that already exists is ensured again
    too, in vain: it is raised after _ENSURE_ATTEMPTS, and `_ensure_unique_key` warns of it.
    """
    if any_cause(failure, lambda it: getattr(it, "sqlstate", None) is not None):
        return any_cause(
            failure,
            lambda it: (
                getattr(it, "sqlstate", None) == _UNIQUE_VIOLATION
                and str(getattr(getattr(it, "diag", None), "constraint_name", None) or "").startswith("pg_")
            ),
        )
    return any_cause(failure, lambda it: bool(_MADE_MEANWHILE.search(str(it))))


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

    async def resolve(self) -> StoreIdentity:
        guarded = await self._ensure_unique_key()
        database = self.persistence_manager.database
        for _ in range(_ATTEMPTS):
            await self._assign_if_absent(guarded)
            stamps = await self.persistence_manager.query(self.backend.store_stamps().spec())
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

    async def _ensure_unique_key(self) -> bool:
        """
        Ensures the uniqueness constraint on the key. Returns whether it is in force; False degrades `resolve` to
        best-effort convergence rather than failing boot over an identity that can still be read.

        Managers making the constraint at once get in each other's way. A deadlock (Neo4j's, on the label's lock) or a
        write turned away is already run again by the schema executor (see `when_not_contended`). One manager's
        statement can also be refused for what another's has just made: the constraint or the key's index already there
        (FalkorDB), or the index's name taken in the catalog (Postgres's `pg_class`, 23505). The constraint is then
        ensured again, which finds the other's. Going on without it would let a stamp be refused by the other's
        constraint, or two stamps be written before it is in force (#298, as drivine4j#473).
        """
        if self.constraints is None:
            return False
        try:
            result = await self._ensured_again_when_made_meanwhile(self.constraints)
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
        if result.status == "BLOCKED_BY_INDEX":
            logger.warning(
                "The index %s on DrivineStore(key) on %s is in the way of its uniqueness constraint. Identity is "
                "best-effort until it is dropped.",
                result.index.name if result.index else None,
                self.persistence_manager.database,
            )
            return False
        return True

    async def _ensured_again_when_made_meanwhile(
        self, constraints: ConstraintManager
    ) -> EnsureResult[UniquenessConstraintSpec]:
        """`ensure`s the key's constraint, again when another manager made it, or what it needs, meanwhile."""
        attempt = 1
        while True:
            try:
                # The label is Drivine's, so a plain index on the key (Neo4j) is replaced by the constraint.
                return await constraints.ensure(_KEY_CONSTRAINT, replace_blocking_index=True)
            except Exception as error:
                # Not a deadlock: the schema executor has run that again already, as many times as it runs one.
                if attempt >= _ENSURE_ATTEMPTS or not _made_meanwhile(error):
                    raise
                await concurrency.sleep(contended_pause(attempt - 1))
                attempt += 1

    async def _assign_if_absent(self, guarded: bool) -> None:
        """
        `MERGE`s the stamp. Under the constraint, a concurrent first writer can make this one fail with a violation or a
        write conflict rather than block; merging again, after a pause, then matches the winner's node.

        A violation is merged again even when `_ensure_unique_key` could not confirm the constraint, for the violation
        itself proves one is in force: managers making it at once can be turned away making it, and a loser's stamp is
        then refused by the winner's constraint (#298, as drivine4j#473). A conflict proves nothing, so it is merged
        again only under a constraint confirmed. A store without schema management (no `constraints`) has no
        constraint to have refused it, and raises at once.
        """
        attempt = 1
        while True:
            try:
                await self.persistence_manager.execute(
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
                if attempt >= _MERGE_ATTEMPTS or not (
                    self._lost_the_race(error) if guarded else self._violated_the_key(error)
                ):
                    raise
                await concurrency.sleep(_MERGE_BACKOFF_SECONDS * 2 ** (attempt - 1))
                attempt += 1

    def _violated_the_key(self, error: BaseException) -> bool:
        """
        Whether a uniqueness constraint refused the stamp: Neo4j's "already exists with label", Memgraph's and
        FalkorDB's "unique constraint violation", Oracle's ORA-00001 "unique constraint violated", Postgres's "violates
        unique constraint". Not another constraint, such as a not-null one, which would refuse it each time it was
        written again. Only a manager with schema management can have made one.
        """
        return self.constraints is not None and bool(_VIOLATED_THE_KEY.search(str(error)))

    def _lost_the_race(self, error: BaseException) -> bool:
        """Whether a peer's boot won: a violation, or a write conflict (Memgraph's, or FalkorDB's "another write")."""
        return self._violated_the_key(error) or bool(_WRITE_CONFLICT.search(str(error)))
