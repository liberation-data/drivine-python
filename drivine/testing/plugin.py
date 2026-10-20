"""
Drivine's pytest plugin. With `drivine_test_containers = true` in the pytest configuration, it starts a container for
each datasource the environment configures, before the tests, and stops them after. See `DrivineTestContainers`.
"""

from __future__ import annotations

import pytest

_KEY = pytest.StashKey[object]()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addini(
        "drivine_test_containers",
        type="bool",
        default=False,
        help="Start a test container for each datasource the environment configures.",
    )


def pytest_sessionstart(session: pytest.Session) -> None:
    if session.config.getini("drivine_test_containers"):
        from drivine.testing.containers import DrivineTestContainers

        session.config.stash[_KEY] = DrivineTestContainers.start()


def pytest_sessionfinish(session: pytest.Session) -> None:
    containers = session.config.stash.get(_KEY, None)
    if containers is not None:
        containers.stop()  # type: ignore[attr-defined]
