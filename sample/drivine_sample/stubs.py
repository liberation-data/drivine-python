"""
The typed references of the sample's models: `drivine_sample/sample_refs.py` and its stub, which drivine's
`python -m drivine.stubs` writes. The build runs `write` (see build.py), and a test runs `write(check=True)`, so they
are never out of date.
"""

from __future__ import annotations

import pkgutil
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
"""The sample's project directory, which the paths below are relative to, as the files record them."""

OUT = "drivine_sample/sample_refs.py"


def model_modules() -> list[str]:
    """Every module of the sample's models, a class to a module, in a stable order."""
    names: list[str] = []
    for package in ("fragment", "view"):
        path = PROJECT / "drivine_sample" / package
        names += sorted(f"drivine_sample.{package}.{it.name}" for it in pkgutil.iter_modules([str(path)]))
    return names


def write(check: bool = False) -> int:
    """Writes the typed references, or with `check` fails (returns 1) when they are out of date."""
    import os

    from drivine.stubs.__main__ import main

    here = Path.cwd()
    os.chdir(PROJECT)
    try:
        return main([*model_modules(), "--out", OUT, *(["--check"] if check else [])])
    finally:
        os.chdir(here)
