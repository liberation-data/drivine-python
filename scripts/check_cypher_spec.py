"""
Checks tests/cypher against the Cypher spec, github.com/liberation-data/drivine-cypher-spec:

- fails when a file differs from the tag tests/cypher/VERSION names, as a case edited here and not in the spec does;
- warns when the spec has a later tag than VERSION, so drivine-python can move to it.

    uv run python scripts/check_cypher_spec.py

CI runs it in its cypher-spec job. drivine4j's scripts/check-cypher-spec.sh, ported.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

from sync_cypher_spec import REPOSITORY, SPEC, spec

TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)")


def say(level: str, message: str) -> None:
    """As GitHub Actions shows a warning or an error, and plain elsewhere."""
    print(f"::{level}::{message}" if os.environ.get("GITHUB_ACTIONS") else f"{level}: {message}")


def version(tag: str) -> tuple[int, int, int] | None:
    """A tag's version, to be ordered by, or None for a tag that isn't vMAJOR.MINOR.PATCH."""
    match = TAG.fullmatch(tag)
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def latest() -> str | None:
    """The spec's latest tag, or None where git can't list its tags: only a warning hangs on it."""
    try:
        listed = subprocess.run(
            ["git", "ls-remote", "--tags", "--refs", f"https://github.com/{REPOSITORY}", "v*"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        say("warning", f"The Cypher spec's tags could not be listed, so a later one isn't looked for: {error}")
        return None
    tags = [line.rsplit("refs/tags/", 1)[-1] for line in listed.splitlines()]
    versioned = [(found, tag) for tag in tags if (found := version(tag)) is not None]
    return max(versioned)[1] if versioned else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.parse_args()

    tag = (SPEC / "VERSION").read_text().strip()
    changed = False
    for name, content in spec(tag).items():
        copy = SPEC / name
        if not copy.is_file() or copy.read_bytes() != content:
            say(
                "error",
                f"tests/cypher/{name} differs from the spec at {tag}. "
                "Change the spec, tag it, and run scripts/sync_cypher_spec.py <tag>.",
            )
            changed = True

    newest = latest()
    ours, theirs = version(tag), version(newest) if newest else None
    if newest and theirs and (ours is None or theirs > ours):
        say(
            "warning",
            f"The Cypher spec is at {newest}; drivine-python runs {tag}. "
            f"Move to it with scripts/sync_cypher_spec.py {newest}.",
        )

    if changed:
        return 1
    print(f"tests/cypher is the spec at {tag}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
