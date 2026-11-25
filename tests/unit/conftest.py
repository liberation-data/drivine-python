import pytest

from drivine.backend.oracle import oracle_indexed_properties


@pytest.fixture
def no_labelled_indexes(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    An Oracle connection reads no labelled index before it compiles a Cypher statement (#508), for a test of what else
    a session is given that counts its statements.
    """
    monkeypatch.setattr(oracle_indexed_properties, "query", lambda graph: None)
