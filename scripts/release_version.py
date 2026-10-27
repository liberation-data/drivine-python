"""
The version arithmetic the release workflows do, on versions written as the tags have them: semantic versions,
`0.3.0` or `0.3.0-rc.1`, ordered by semver precedence, so that a prerelease comes before its release. (`sort -V`
puts `0.3.0-rc.1` after `0.3.0`, and PEP 440's order needs the versions normalized first.)

    python scripts/release_version.py bump 0.3.0 minor        # prints 0.4.0
    git tag -l | python scripts/release_version.py newest     # prints the newest vX.Y.Z tag, or nothing
    python scripts/release_version.py greater 0.4.0 0.3.0     # exits 0 when the first is the greater, 1 when not
    python scripts/release_version.py set pyproject.toml 0.4.0

A bump works as npm's semver does, with `rc` for a prerelease: `patch`, `minor` and `major` of a prerelease give
the release it leads to (`0.4.0-rc.1` minor is `0.4.0`), and `prerelease` counts on a prerelease (`0.4.0-rc.1` to
`0.4.0-rc.2`) or starts the next patch's (`0.3.0` to `0.3.1-rc.0`).

It reads the standard library alone, so nothing is installed for it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z.-]+))?$")
TAG = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$")
PROJECT_VERSION = re.compile(r'^version = "[^"]*"$', re.MULTILINE)


class Version:
    def __init__(self, text: str) -> None:
        match = SEMVER.match(text)
        if match is None or (match.group(4) is not None and "" in match.group(4).split(".")):
            raise ValueError(f"{text} isn't a version X.Y.Z or X.Y.Z-suffix")
        self.major, self.minor, self.patch = (int(match.group(n)) for n in (1, 2, 3))
        self.prerelease: list[str] = match.group(4).split(".") if match.group(4) else []

    def __str__(self) -> str:
        release = f"{self.major}.{self.minor}.{self.patch}"
        return f"{release}-{'.'.join(self.prerelease)}" if self.prerelease else release

    def key(self) -> tuple[int, int, int, int, list[tuple[int, int, str]]]:
        # A release sorts after all of its prereleases. Within a prerelease, a numeric identifier sorts before an
        # alphanumeric one, numbers by value and words by ASCII, and a shorter list before a longer one it begins.
        identifiers = [(0, int(i), "") if i.isdigit() else (1, 0, i) for i in self.prerelease]
        return (self.major, self.minor, self.patch, 0 if self.prerelease else 1, identifiers)


def bump(version: Version, kind: str) -> Version:
    major, minor, patch, prerelease = version.major, version.minor, version.patch, version.prerelease
    if kind == "major":
        if not (prerelease and minor == 0 and patch == 0):
            major, minor, patch = major + 1, 0, 0
        prerelease = []
    elif kind == "minor":
        if not (prerelease and patch == 0):
            minor, patch = minor + 1, 0
        prerelease = []
    elif kind == "patch":
        if not prerelease:
            patch += 1
        prerelease = []
    elif kind == "prerelease":
        if not prerelease:
            patch, prerelease = patch + 1, ["rc", "0"]
        elif prerelease[-1].isdigit():
            prerelease = [*prerelease[:-1], str(int(prerelease[-1]) + 1)]
        else:
            prerelease = [*prerelease, "0"]
    else:
        raise ValueError(f"{kind} isn't patch, minor, major or prerelease")
    return Version(f"{major}.{minor}.{patch}" + (f"-{'.'.join(prerelease)}" if prerelease else ""))


def newest(tags: list[str]) -> str | None:
    versions = [Version(tag[1:]) for tag in tags if TAG.match(tag) and SEMVER.match(tag[1:])]
    return f"v{max(versions, key=Version.key)}" if versions else None


def set_version(pyproject: Path, version: Version) -> None:
    text = pyproject.read_text()
    if len(PROJECT_VERSION.findall(text)) != 1:
        raise ValueError(f'{pyproject} doesn\'t have one line version = "..."')
    pyproject.write_text(PROJECT_VERSION.sub(f'version = "{version}"', text))


def main(args: list[str]) -> int:
    try:
        match args:
            case ["bump", version, kind]:
                print(bump(Version(version), kind))
            case ["newest"]:
                found = newest(sys.stdin.read().split())
                if found:
                    print(found)
            case ["greater", first, second]:
                return 0 if Version(first).key() > Version(second).key() else 1
            case ["set", pyproject, version]:
                set_version(Path(pyproject), Version(version))
            case _:
                print(__doc__, file=sys.stderr)
                return 2
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
