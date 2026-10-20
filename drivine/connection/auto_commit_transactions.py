from __future__ import annotations

import logging

from drivine.connection.transaction_mode import TransactionMode
from drivine.drivine_error import DrivineError


class AutoCommitTransactions:
    """
    Transaction handling for a connection to an engine that commits each statement as it runs, such as FalkorDB. A
    connection hands its start, commit and rollback here.
    """

    def __init__(self, engine: str, mode: TransactionMode = TransactionMode.WARN) -> None:
        self.engine = engine
        self.mode = mode
        self._logger = logging.getLogger(f"drivine.connection.{engine.lower()}")

    def start(self) -> None:
        if self.mode is TransactionMode.STRICT:
            raise DrivineError(
                f"{self.engine} does not support multi-statement transactions. Remove @transactional, or set "
                f"transaction_mode to WARN to allow each statement to commit as it runs."
            )
        self._logger.debug(
            "%s transaction started (passthrough: each statement commits as it runs, no atomicity).", self.engine
        )

    def commit(self) -> None:
        self._logger.debug("%s transaction committed (passthrough: writes already committed).", self.engine)

    def rollback(self) -> None:
        """Never raises, in either mode: it is called on the error path, and mustn't hide the error that caused it."""
        self._logger.warning(
            "%s rollback requested, but each statement was committed as it ran. Nothing was rolled back.", self.engine
        )
