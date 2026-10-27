"""scripts/release_version.py: the semver arithmetic the release workflows do. A prerelease comes before its release,
which `sort -V` gets the wrong way round."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_path = Path(__file__).parents[2] / "scripts" / "release_version.py"
_spec = importlib.util.spec_from_file_location("release_version", _path)
assert _spec is not None and _spec.loader is not None
release_version = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_version)


@pytest.mark.parametrize(
    ("version", "kind", "expected"),
    [
        ("0.2.0", "patch", "0.2.1"),
        ("0.2.0", "minor", "0.3.0"),
        ("0.2.0", "major", "1.0.0"),
        ("0.2.0", "prerelease", "0.2.1-rc.0"),
        ("0.3.1-rc.0", "prerelease", "0.3.1-rc.1"),
        ("0.3.1-rc.0", "patch", "0.3.1"),
        ("0.4.0-rc.1", "minor", "0.4.0"),
        ("0.4.0-rc.1", "major", "1.0.0"),
        ("1.0.0-rc.1", "major", "1.0.0"),
        ("1.2.0-rc.1", "major", "2.0.0"),
        ("1.2.3-rc.0", "minor", "1.3.0"),
        ("0.4.0-beta.2", "prerelease", "0.4.0-rc.0"),
        ("0.4.0-rc", "prerelease", "0.4.0-rc.0"),
        ("0.4.0-rc.alpha", "prerelease", "0.4.0-rc.0"),
        ("0.4.0-rc.1.fix", "prerelease", "0.4.0-rc.2.fix"),
    ],
)
def test_bump(version: str, kind: str, expected: str) -> None:
    assert str(release_version.bump(release_version.Version(version), kind)) == expected


@pytest.mark.parametrize(
    ("first", "second", "greater"),
    [
        ("0.3.0", "0.3.0-rc.1", True),
        ("0.3.0-rc.1", "0.3.0", False),
        ("0.3.0-rc.10", "0.3.0-rc.2", True),
        ("0.3.0-rc.1", "0.3.0-rc", True),
        ("0.10.0", "0.9.0", True),
        ("0.2.0", "0.2.0", False),
        ("1.0.0-alpha.beta", "1.0.0-alpha.1", True),
        ("1.0.0-alpha.1", "1.0.0-alpha", True),
        ("1.0.0-beta.11", "1.0.0-beta.2", True),
    ],
)
def test_greater(first: str, second: str, greater: bool) -> None:
    assert (release_version.main(["greater", first, second]) == 0) is greater


def test_newest_tag_by_semver() -> None:
    tags = ["v0.1.0", "v0.3.0-rc.1", "v0.3.0", "0.9.0", "vnext", "v0.2.0"]
    assert release_version.newest(tags) == "v0.3.0"
    assert release_version.newest(["v0.3.0", "v0.3.1-rc.0"]) == "v0.3.1-rc.0"
    assert release_version.newest([]) is None
    assert release_version.newest(["v0.2.0", "v0.9.0-rc..1", "v09.0.0"]) == "v0.2.0"


def test_refuses_what_isnt_a_version() -> None:
    with pytest.raises(ValueError):
        release_version.Version("0.3")
    with pytest.raises(ValueError):
        release_version.Version("0.3.0rc1")


def test_set_rewrites_the_project_version(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "drivine"\nversion = "0.2.0"\n\n[tool.x]\ntarget-version = "py311"\n')
    release_version.set_version(pyproject, release_version.Version("0.3.0-rc.1"))
    assert 'version = "0.3.0-rc.1"' in pyproject.read_text()
    assert 'target-version = "py311"' in pyproject.read_text()
