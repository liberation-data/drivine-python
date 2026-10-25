"""
Copies a tagged version of the Cypher spec, github.com/liberation-data/drivine-cypher-spec, to tests/cypher unchanged:
its queries.yaml, writes.yaml, temporal.yaml, refusals.yaml and README.md, which says what they hold. The tag is
recorded in tests/cypher/VERSION.

    uv run python scripts/sync_cypher_spec.py v0.3.0

The three Drivine libraries run the same files, so a Cypher statement gives the same rows from each: run this when the
spec is tagged again, and tests/integration/test_cypher_spec.py and tests/unit/test_cypher_refusals.py run what it
says. scripts/check_cypher_spec.py checks the copies are the tag's. drivine4j's scripts/sync-cypher-spec.sh,
ported.
"""

from __future__ import annotations

import argparse
import io
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "tests" / "cypher"
REPOSITORY = "liberation-data/drivine-cypher-spec"
FILES = (
    "queries.yaml",
    "writes.yaml",
    "temporal.yaml",
    "refusals.yaml",
    "README.md",
    "model/schema.json",
    "model/README.md",
)
"""The spec's cases, and the model file's format, which the stubs step writes to."""


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url) as response:
        return response.read()


def spec(tag: str) -> dict[str, bytes]:
    """
    Each of FILES at the spec's tag, by its name. Every file is read before any is given, so a tag without one fails
    here, and nothing is written.
    """
    archive = fetch(f"https://github.com/{REPOSITORY}/archive/refs/tags/{tag}.tar.gz")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        # Each file by its name under the archive's one folder: nothing else in the archive is read.
        members = {
            Path(it.name).relative_to(Path(it.name).parts[0]).as_posix(): it for it in tar.getmembers() if it.isfile()
        }
        contents: dict[str, bytes] = {}
        for name in FILES:
            content = tar.extractfile(members[name]) if name in members else None
            if content is None:
                raise SystemExit(f"{name} is not a file in {REPOSITORY}@{tag}.")
            contents[name] = content.read()
    return contents


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("tag", help="a tag of the Cypher spec, such as v0.3.0")
    args = parser.parse_args()

    for name, content in spec(args.tag).items():
        (SPEC / name).parent.mkdir(parents=True, exist_ok=True)
        (SPEC / name).write_bytes(content)
    (SPEC / "VERSION").write_text(f"{args.tag}\n")
    print(f"tests/cypher is now {REPOSITORY}@{args.tag}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
