"""
Poetry's build script: `poetry build` and `poetry install` run it, and it writes the typed references of the sample's
models with drivine's stub generator (`python -m drivine.stubs`), so they always match the models.

Poetry runs it in an isolated environment holding the build-system requirements. An application that takes drivine
from PyPI lists `drivine` there; the sample, which lives in drivine's repository, lists drivine's own dependencies and
reads drivine from the repository instead.
"""

import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))
try:
    import drivine  # noqa: F401
except ImportError:
    sys.path.insert(1, str(PROJECT.parent))

from drivine_sample.stubs import write  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(write())
