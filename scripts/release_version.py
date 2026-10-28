"""
The version arithmetic the release workflows do, on versions written as the tags have them: semantic versions,
`0.3.0` or `0.3.0-rc.1`, ordered by semver precedence, so that a prerelease comes before its release. (`sort -V`
puts `0.3.0-rc.1` after `0.3.0`, and PEP 440's order needs the versions normalized first.)

    python scripts/release_version.py bump 0.3.0 minor        # prints 0.4.0
    git tag -l | python scripts/release_version.py newest     # prints the newest vX.Y.Z tag, or nothing
    python scripts/release_version.py greater 0.4.0 0.3.0     # exits 0 when the first is the greater, 1 when not
    python scripts/release_version.py set pyproject.toml 0.4.0
    python scripts/release_version.py references 0.4.0       # the other files that name it; prints their paths

A bump works as npm's semver does, with `rc` for a prerelease: `patch`, `minor` and `major` of a prerelease give
the release it leads to (`0.4.0-rc.1` minor is `0.4.0`), and `prerelease` counts on a prerelease (`0.4.0-rc.1` to
`0.4.0-rc.2`) or starts the next patch's (`0.3.0` to `0.3.1-rc.0`), as `npm version prerelease --preid rc` does: a
prerelease that isn't `rc.N` moves to `rc.0` (`0.4.0-beta.2` to `0.4.0-rc.0`).

`set` writes the version in pyproject.toml, and `uv lock` then writes it in uv.lock. Other files name the version
too, and `references` rewrites them, from the list in REFERENCES below: the sample's model file, whose generator
version the sample's test of its typed references compares (sample/tests/test_typed_refs_up_to_date.py), the
sample's poetry.lock, and the README's line on which release this is. Each is matched by a pattern tight enough that
it can't hit another package's version, and must be found exactly once, so that a file moved or a line reworded
fails the release PR here, rather than leaving it to name the old version.

It reads the standard library alone, so nothing is installed for it.
"""

from __future__ import annotations

import re
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path

SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z.-]+))?$")
TAG = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$")
PROJECT_VERSION = re.compile(r'^version = "[^"]*"$', re.MULTILINE)
# A prerelease suffix as PEP 440 reads it, after the tag's "-", with the spellings it normalizes: X.Y.Z-rc.1 is
# X.Y.Zrc1, X.Y.Z-beta.2 is X.Y.Zb2, X.Y.Z-dev.3 is X.Y.Z.dev3, and a bare number, X.Y.Z-1, is the post-release
# X.Y.Z.post1. A missing number is 0.
PEP440_SUFFIX = re.compile(
    r"^(?:(?P<pre>alpha|a|beta|b|c|rc|preview|pre)[-_.]?(?P<pre_n>[0-9]+)?)?"
    r"(?:(?:[-_.]?(?P<post>post|rev|r)[-_.]?(?P<post_n>[0-9]+)?)|(?P<implicit_post>[0-9]+))?"
    r"(?:[-_.]?(?P<dev>dev)[-_.]?(?P<dev_n>[0-9]+)?)?$",
    re.IGNORECASE,
)
PRE_LABELS = {"alpha": "a", "a": "a", "beta": "b", "b": "b", "c": "rc", "rc": "rc", "preview": "rc", "pre": "rc"}


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
        # As npm's `prerelease --preid rc`: count the last numeric identifier up, or add a 0 when there is none; then
        # a prerelease that isn't rc.N starts over at rc.0.
        if not prerelease:
            patch, prerelease = patch + 1, ["0"]
        else:
            numeric = [n for n, identifier in enumerate(prerelease) if identifier.isdigit()]
            if numeric:
                prerelease = list(prerelease)
                prerelease[numeric[-1]] = str(int(prerelease[numeric[-1]]) + 1)
            else:
                prerelease = [*prerelease, "0"]
        if prerelease[0] != "rc" or not prerelease[1].isdigit():
            prerelease = ["rc", "0"]
    else:
        raise ValueError(f"{kind} isn't patch, minor, major or prerelease")
    return Version(f"{major}.{minor}.{patch}" + (f"-{'.'.join(prerelease)}" if prerelease else ""))


def newest(tags: list[str]) -> str | None:
    versions: list[Version] = []
    for tag in tags:
        if TAG.match(tag):
            try:
                versions.append(Version(tag[1:]))
            except ValueError:
                pass  # v01.2.3 or v1.2.3-rc..1: a tag, but not a semantic version, so no release's.
    return f"v{max(versions, key=Version.key)}" if versions else None


def pep440(version: Version) -> str:
    """
    The version as PEP 440 normalizes it, as uv.lock and poetry.lock record it: 0.3.0-rc.1 is 0.3.0rc1. The wheel's
    own metadata keeps the version as pyproject.toml writes it, so a model file records 0.3.0-rc.1.
    """
    release = f"{version.major}.{version.minor}.{version.patch}"
    if not version.prerelease:
        return release
    suffix = ".".join(version.prerelease)
    match = PEP440_SUFFIX.match(suffix)
    if match is None:
        raise ValueError(f"{version}'s suffix isn't one PEP 440 reads, such as rc.1, beta.2 or dev.3")
    out = release
    if match["pre"]:
        out += f"{PRE_LABELS[match['pre'].lower()]}{int(match['pre_n'] or 0)}"
    if match["post"] or match["implicit_post"]:
        out += f".post{int(match['post_n'] or match['implicit_post'] or 0)}"
    if match["dev"]:
        out += f".dev{int(match['dev_n'] or 0)}"
    return out


class Reference:
    """
    One file's mention of the version, besides pyproject.toml's and uv.lock's. The pattern has two groups, the text
    before the version and the text after it, and matches the version between them; `write` gives what replaces it.
    """

    def __init__(self, path: str, pattern: str, write: Callable[[Version], str], flags: int = 0) -> None:
        self.path = path
        self.pattern = re.compile(pattern, flags)
        self.write = write


# Every file but pyproject.toml and uv.lock that names drivine's own version, and moves with each release. A file
# that names it only as history, as RELEASING.md's examples do, isn't listed. tests/unit/test_release_version.py
# checks that every model file in the repository is listed.
REFERENCES = [
    # The generator the model file names: the version importlib.metadata reads from drivine's metadata, as
    # pyproject.toml writes it. The sample's test of its typed references compares the whole file, so it fails on a
    # model file that names another version.
    Reference(
        "sample/.drivine/model.json",
        r'("generator": \{\s*"library": "drivine-python",\s*"version": ")[^"]*(")',
        str,
    ),
    # The sample's lock records drivine, which it takes from this repository, at the version PEP 440 normalizes it
    # to, as `poetry lock` writes it. Its other packages are left alone.
    Reference(
        "sample/poetry.lock", r'(^\[\[package\]\]\nname = "drivine"\nversion = ")[^"\n]*(")', pep440, re.MULTILINE
    ),
    # "0.2 is an alpha release": the MAJOR.MINOR, of a prerelease too.
    Reference(
        "README.md",
        r"((?<![0-9.]))[0-9]+\.[0-9]+( is an alpha release:)",
        lambda version: f"{version.major}.{version.minor}",
    ),
]


def set_references(root: Path, version: Version, references: list[Reference] = REFERENCES) -> list[str]:
    """
    Rewrites each reference under `root` to name `version`, and gives the paths of the files. Every reference is
    checked before any file is written: one that isn't found exactly once fails, and nothing is changed.
    """
    changed: dict[Path, str] = {}
    for reference in references:
        path = root / reference.path
        if not path.is_file():
            raise ValueError(f"{reference.path} names the version, but there is no such file. Update REFERENCES.")
        text = changed.get(path, path.read_text())
        found = len(reference.pattern.findall(text))
        if found != 1:
            raise ValueError(
                f"{reference.path} should name the version once, matching {reference.pattern.pattern}, but has "
                f"{found} matches. Update REFERENCES to match the file."
            )
        # A version is digits, letters, dots and dashes, so it can't hold a backreference.
        changed[path] = reference.pattern.sub(rf"\g<1>{reference.write(version)}\g<2>", text)
    for path, text in changed.items():
        path.write_text(text)
    return list(dict.fromkeys(reference.path for reference in references))


def set_version(pyproject: Path, version: Version) -> None:
    text = pyproject.read_text()
    if len(PROJECT_VERSION.findall(text)) != 1:
        raise ValueError(f'{pyproject} doesn\'t have one line version = "..."')
    changed = PROJECT_VERSION.sub(f'version = "{version}"', text)
    # The line must be [project]'s version, not another table's.
    if tomllib.loads(changed).get("project", {}).get("version") != str(version):
        raise ValueError(f"{pyproject}'s line version = \"...\" isn't [project]'s version")
    pyproject.write_text(changed)


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
            case ["references", version]:
                for path in set_references(Path(__file__).resolve().parents[1], Version(version)):
                    print(path)
            case _:
                print(__doc__, file=sys.stderr)
                return 2
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
