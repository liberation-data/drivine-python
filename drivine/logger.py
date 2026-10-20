from __future__ import annotations

import logging
import os
import time
from typing import Any

statements = logging.getLogger("drivine.statements")
"""Each statement Drivine runs, at DEBUG, with its session and how long it took."""

_LEVELS = {
    "VERBOSE": logging.DEBUG,
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARN": logging.WARNING,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "NONE": logging.CRITICAL + 1,
}


def log_statement(session_id: str, spec: Any, start: float) -> None:
    if statements.isEnabledFor(logging.DEBUG):
        elapsed = (time.perf_counter() - start) * 1000
        statement = spec.statement.text if spec.statement else None
        statements.debug(
            "session=%s elapsed=%.3f ms statement=%r parameters=%r", session_id, elapsed, statement, spec.parameters
        )


def _honour_env_level() -> None:
    """
    `DRIVINE_LOG_LEVEL`, as TypeScript's Drivine reads it, sets the `drivine` logger's level, unless the
    application set one.
    """
    level = os.environ.get("DRIVINE_LOG_LEVEL")
    drivine = logging.getLogger("drivine")
    if level and level.upper() in _LEVELS and drivine.level == logging.NOTSET:
        drivine.setLevel(_LEVELS[level.upper()])


_honour_env_level()
