"""The shared module's SQLite extension, loaded into each connection and checked against the compiler."""

from __future__ import annotations

import apsw
import pytest

from drivine.backend.sqlite import sqlite_extension
from drivine.drivine_error import DrivineError


def test_a_connection_is_given_the_modules_functions_of_this_librarys_host_version():
    connection = apsw.Connection(":memory:")
    try:
        sqlite_extension.load(connection)
        found = connection.execute("SELECT drivine_host_version(), drivine_equal('[1]', '[1.0]')").fetchall()
        assert found == [(sqlite_extension.HOST_VERSION, 1)]
    finally:
        connection.close()


def test_the_library_stays_loaded_once_a_connection_that_loaded_it_closes():
    # SQLite closes an extension's library with the connection: held open, the module's runtime outlives it.
    for _ in range(3):
        connection = apsw.Connection(":memory:")
        sqlite_extension.load(connection)
        connection.close()
    assert sqlite_extension._held is not None  # pyright: ignore[reportPrivateUsage]
    connection = apsw.Connection(":memory:")
    try:
        sqlite_extension.load(connection)
        assert connection.execute("SELECT drivine_sort_key('[1]') IS NOT NULL").fetchall() == [(1,)]
    finally:
        connection.close()


def test_functions_of_another_host_version_are_refused(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sqlite_extension, "HOST_VERSION", sqlite_extension.HOST_VERSION + 1)
    connection = apsw.Connection(":memory:")
    try:
        with pytest.raises(DrivineError, match="host version 2, and this Drivine's statements need 3"):
            sqlite_extension.load(connection)
    finally:
        connection.close()


def test_an_extension_of_another_version_than_the_compiler_is_refused(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sqlite_extension.importlib.metadata, "version", lambda _: "0.0.1")
    connection = apsw.Connection(":memory:")
    try:
        with pytest.raises(DrivineError, match="and its compiler 0.0.1"):
            sqlite_extension.load(connection)
    finally:
        connection.close()
