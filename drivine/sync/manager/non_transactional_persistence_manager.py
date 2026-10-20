# Written by scripts/unasync.py from drivine/manager/non_transactional_persistence_manager.py:
# change that, and run the script again.
from __future__ import annotations

from typing import TypeVar

from drivine.cursor.cursor_specification import CursorSpecification
from drivine.drivine_error import DrivineError
from drivine.query.query_specification import QuerySpecification
from drivine.sync.connection.connection import Connection
from drivine.sync.connection.connection_provider import ConnectionProvider
from drivine.sync.cursor.cursor import Cursor
from drivine.sync.manager.persistence_manager import PersistenceManager

T = TypeVar("T")


class NonTransactionalPersistenceManager(PersistenceManager):
    """
    Runs each statement on a connection of its own, which commits it.
    """

    def __init__(self, provider: ConnectionProvider, database: str) -> None:
        self.provider = provider
        self.database = database
        self.type = provider.type

    def query(self, spec: QuerySpecification[T]) -> list[T]:
        connection = self.provider.connect()
        try:
            results = connection.query(spec)
        except Exception as error:
            connection.release(error)
            raise DrivineError.with_root_cause(error, spec) from error
        except BaseException as error:
            connection.release(error)
            raise
        connection.release()
        return results

    def open_cursor(self, spec: CursorSpecification[T]) -> Cursor[T]:
        connection = self.provider.connect()
        try:
            cursor = connection.open_cursor(spec)
        except BaseException as error:
            connection.release(error)
            if isinstance(error, Exception):
                raise DrivineError.with_root_cause(error, spec) from error
            raise
        return _ConnectionCursor(cursor, connection)


class _ConnectionCursor(Cursor[T]):
    """
    A cursor outside a transaction, on a connection of its own, which it releases when it is closed.
    """

    def __init__(self, cursor: Cursor[T], connection: Connection) -> None:
        self.cursor = cursor
        self.connection = connection
        self._released = False

    def __next__(self) -> T:
        try:
            return self.cursor.__next__()
        except StopIteration:
            raise
        except Exception as error:
            self.close(error)
            raise DrivineError.with_root_cause(error) from error

    def close(self, error: BaseException | None = None) -> None:
        if self._released:
            return
        self._released = True
        try:
            self.cursor.close()
        finally:
            self.connection.release(error)
