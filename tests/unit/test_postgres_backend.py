"""What the Postgres backend reads of the store's answers: contention, and a schema marker's inventory."""

from drivine.backend.postgres.postgres_backend import PostgresBackend


class SqlError(Exception):
    """An error as psycopg raises one: with the SQLSTATE Postgres gave."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def caused_by(cause: BaseException) -> Exception:
    try:
        raise RuntimeError("wrapped") from cause
    except RuntimeError as error:
        return error


def test_a_serialization_failure_or_a_deadlock_is_contention_however_deep_it_is_wrapped():
    backend = PostgresBackend()
    assert backend.contended(SqlError("40001"))
    assert backend.contended(caused_by(caused_by(SqlError("40P01"))))


def test_any_other_failure_is_not_contention():
    backend = PostgresBackend()
    assert not backend.contended(SqlError("23505"))
    assert not backend.contended(caused_by(ValueError("40001")))


def test_keeps_an_empty_inventory_apart_from_none():
    assert PostgresBackend.marker_items(None) is None
    assert PostgresBackend.marker_items("") == []
    assert PostgresBackend.marker_items("a\nb") == ["a", "b"]
