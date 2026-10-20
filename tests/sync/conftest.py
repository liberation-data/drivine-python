"""The sync API's own tests, beside those scripts/unasync.py writes: what only threads do."""

from tests.sync_integration.conftest import factory, pm, pytest_collection_modifyitems, registry

__all__ = ["factory", "pm", "pytest_collection_modifyitems", "registry"]
