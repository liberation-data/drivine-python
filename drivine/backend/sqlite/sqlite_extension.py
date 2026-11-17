# apsw's stubs type a bytes value as collections.abc.Buffer, which Python 3.11 lacks: what they give is partly unknown.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
"""
The shared module's SQLite extension, in the `drivine-cypher-compiler` wheel beside its compiler (`library_path()`,
entry `sqlite3_drivinecypher_init`): the host functions compiled Cypher calls on SQLite, as drivine4j and Drivine for
TypeScript run them, so all three compare, add, order, round, match and work out temporal values alike. host.yaml pins
them. drivine-python registers the rest itself (see sqlite_functions): the write plan, deletes, lowercased copies,
similarity, a statement's procedure, and the string and math functions the module leaves to each library.

Loaded into each connection as it opens, into apsw's own SQLite, the one the process binds it to. Its host version must
be this library's, and its module version the compiler's, so a statement compiled by one module never runs against
another's functions.
"""

from __future__ import annotations

import ctypes
import importlib.metadata
import threading
from typing import Any

import apsw

from drivine.drivine_error import DrivineError

ENTRY = "sqlite3_drivinecypher_init"
"""The extension's entry point in the module's library."""

HOST_VERSION = 2
"""The version of the host functions' forms compiled SQL expects: the module's `SqliteFunctions.HOST_VERSION`."""

PACKAGE = "drivine-cypher-compiler"

_held: Any = None
_holding = threading.Lock()


def _hold(path: str) -> None:
    """
    Opens the module's library once for the process, and keeps it open. SQLite closes an extension's library with the
    connection that loaded it, and the module's Kotlin/Native runtime keeps threads of its own (its collector's) running
    the library's code: closed under them, the process faults, and loaded again, it starts another runtime. Held open
    here, SQLite's close leaves it loaded, and every connection's load finds the one runtime.
    """
    global _held
    if _held is None:
        with _holding:
            if _held is None:
                _held = ctypes.CDLL(path)


def load(connection: apsw.Connection) -> None:
    """Loads the extension into `connection` and checks it is the one the compiler's statements expect."""
    try:
        import drivine_cypher_compiler
    except ModuleNotFoundError as error:
        if error.name != "drivine_cypher_compiler":
            raise
        from drivine.cypher.shared_compiler import not_installed

        raise DrivineError(not_installed("SQLITE")) from error
    path = drivine_cypher_compiler.library_path()
    _hold(path)
    connection.enable_load_extension(True)
    try:
        connection.load_extension(path, ENTRY)
    finally:
        connection.enable_load_extension(False)
    found: Any = connection.execute("SELECT drivine_host_version(), drivine_extension_version()").fetchall()
    host, module = found[0]
    if host != HOST_VERSION:
        raise DrivineError(
            f"The shared module's SQLite functions are of host version {host}, and this Drivine's statements need "
            f"{HOST_VERSION}: install the drivine-cypher-compiler this Drivine names."
        )
    expected = importlib.metadata.version(PACKAGE)
    if module != expected:
        raise DrivineError(
            f"The shared module's SQLite extension is of version {module}, and its compiler {expected}: reinstall "
            "drivine-cypher-compiler, whose library holds both."
        )
