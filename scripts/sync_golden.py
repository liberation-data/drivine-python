"""
Copies the golden statements TypeScript's Drivine writes, its test/1.Unit/golden/statements.json, to
tests/golden/statements.json unchanged, and records the commit they were copied from in tests/golden/VERSION.

Give it a tag, a branch or a commit of github.com/liberation-data/drivine:

    uv run python scripts/sync_golden.py v3.2.0

Run it whenever TypeScript's Drivine writes the goldens again (UPDATE_GOLDEN=true): the statements this backend
writes are then checked against those, by tests/unit/test_golden_statements.py.
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "golden"
REPOSITORY = "liberation-data/drivine"
SOURCE = "test/1.Unit/golden/statements.json"


def fetch(url: str, accept: str | None = None) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": accept} if accept else {})
    with urllib.request.urlopen(request) as response:
        return response.read()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("ref", help="a tag, a branch or a commit of TypeScript's Drivine")
    args = parser.parse_args()

    # The commit the ref names now, so VERSION says what the goldens are, whatever the branch does after.
    sha = "application/vnd.github.sha"
    commit = fetch(f"https://api.github.com/repos/{REPOSITORY}/commits/{args.ref}", sha).decode()
    (GOLDEN / "statements.json").write_bytes(fetch(f"https://raw.githubusercontent.com/{REPOSITORY}/{commit}/{SOURCE}"))
    (GOLDEN / "VERSION").write_text(f"{commit}\n")
    print(f"tests/golden/statements.json is now {REPOSITORY}@{commit} ({args.ref}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
