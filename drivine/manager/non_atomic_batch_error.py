from __future__ import annotations

from drivine.drivine_error import DrivineError


class NonAtomicBatchError(DrivineError):
    """
    A batch of several statements can't be applied whole on an engine without multi-statement transactions, such as
    FalkorDB, and its transaction_mode is STRICT. Nothing was written.
    """

    def __init__(self, engine: str, statements: int) -> None:
        super().__init__(
            f"A batch of {statements} statements can't be applied whole on {engine}, which has no multi-statement "
            f"transactions, and transaction_mode is STRICT. Nothing was written. Save fewer objects at a time, or set "
            f"transaction_mode to WARN to run the batch statement by statement."
        )
