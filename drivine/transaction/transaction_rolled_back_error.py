from __future__ import annotations

from drivine.drivine_error import DrivineError


class TransactionRolledBackError(DrivineError):
    """
    The transaction is rolling back, because a statement in it failed or an exception left a `@transactional` call,
    though the caller caught it. A later statement in the transaction raises it at once, and so does the end of the
    transaction that was meant to commit. `__cause__` is the first failure.
    """
