"""scripts/unasync.py: what it leaves out of the sync code."""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

_path = Path(__file__).parents[2] / "scripts" / "unasync.py"
_spec = importlib.util.spec_from_file_location("unasync", _path)
assert _spec is not None and _spec.loader is not None
unasync = importlib.util.module_from_spec(_spec)
sys.modules["unasync"] = unasync  # its dataclass looks itself up there
_spec.loader.exec_module(unasync)

SOURCE = """class Kept:
    def close(self):
        return 1

    def keep(self):
        return 2


class Assembly:
    @property
    def close(self):
        return 3

    def keep(self):
        return 4


def close():
    return 5
"""


def test_async_only_leaves_out_exactly_the_named_definitions(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(unasync, "ASYNC_ONLY", {"m": ("Assembly.close",)})
    text = unasync.without_async_only(SOURCE, ast.parse(SOURCE), "m")
    assert "return 3" not in text and "@property" not in text
    assert all(f"return {it}" in text for it in (1, 2, 4, 5))
    assert unasync.without_async_only(SOURCE, ast.parse(SOURCE), "other") == SOURCE


def test_async_only_refuses_a_name_the_module_does_not_define(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(unasync, "ASYNC_ONLY", {"m": ("Assembly.closed",)})
    with pytest.raises(SystemExit, match="Assembly.closed"):
        unasync.without_async_only(SOURCE, ast.parse(SOURCE), "m")
