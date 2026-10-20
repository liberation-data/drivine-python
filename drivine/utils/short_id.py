from __future__ import annotations

import secrets


def short_id() -> str:
    """A short id for a session or transaction, for the logs."""
    return secrets.token_hex(4)[:7]
