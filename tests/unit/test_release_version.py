"""scripts/release_version.py: the semver arithmetic the release workflows do. A prerelease comes before its release,
which `sort -V` gets the wrong way round."""

from __future__ import annotations

import importlib.util
import os
import tomllib
from pathlib import Path

import packaging.version
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


def test_set_refuses_another_tables_version(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    original = '[project]\nname = "drivine"\nversion="0.2.0"\n\n[tool.x]\nversion = "1.0"\n'
    pyproject.write_text(original)
    with pytest.raises(ValueError):
        release_version.set_version(pyproject, release_version.Version("0.3.0"))
    assert pyproject.read_text() == original


ROOT = Path(__file__).parents[2]

MODEL = """{
  "$comment": "Drivine model file",
  "format": "drivine-model/1",
  "generator": {
    "library": "drivine-python",
    "version": "0.2.0",
    "language": "python"
  },
  "module": "drivine-sample",
  "fragments": [{"id": "x", "properties": [{"name": "version", "default": "0.2.0"}]}]
}
"""

POETRY_LOCK = """[[package]]
name = "drivine"
version = "0.2.0"
description = "Graph database client"

[package.dependencies]
pi-pilgrim = ">=0.2.0"

[[package]]
name = "drivine-extra"
version = "0.2.0"

[[package]]
name = "pi-pilgrim"
version = "0.2.0"
"""

README = """# Drivine for Python

0.2 is an alpha release: the API may still change.

The design names pi-pilgrim 0.2, and version 0.2.0 of a dependency.
"""


def a_project(tmp_path: Path, model: str = MODEL, lock: str = POETRY_LOCK, readme: str = README) -> Path:
    (tmp_path / "sample" / ".drivine").mkdir(parents=True)
    (tmp_path / "sample" / ".drivine" / "model.json").write_text(model)
    (tmp_path / "sample" / "poetry.lock").write_text(lock)
    (tmp_path / "README.md").write_text(readme)
    return tmp_path


def test_references_rewrites_each_and_nothing_else(tmp_path: Path) -> None:
    root = a_project(tmp_path)
    changed = release_version.set_references(root, release_version.Version("0.3.0"))
    assert changed == ["sample/.drivine/model.json", "sample/poetry.lock", "README.md"]
    assert (root / "sample" / ".drivine" / "model.json").read_text() == MODEL.replace(
        '"version": "0.2.0"', '"version": "0.3.0"'
    )
    # drivine's own entry moves; a package of a name that begins drivine's, and a dependency's version, don't.
    assert (root / "sample" / "poetry.lock").read_text() == POETRY_LOCK.replace(
        'name = "drivine"\nversion = "0.2.0"', 'name = "drivine"\nversion = "0.3.0"'
    )
    assert (root / "README.md").read_text() == README.replace("0.2 is an alpha", "0.3 is an alpha")


def test_references_of_a_prerelease(tmp_path: Path) -> None:
    root = a_project(tmp_path)
    release_version.set_references(root, release_version.Version("0.4.0-rc.1"))
    # The model file has the version as drivine's metadata does, as pyproject.toml writes it; poetry.lock has it as
    # PEP 440 normalizes it; the README has the release it leads to.
    assert '"version": "0.4.0-rc.1"' in (root / "sample" / ".drivine" / "model.json").read_text()
    assert 'name = "drivine"\nversion = "0.4.0rc1"' in (root / "sample" / "poetry.lock").read_text()
    assert "0.4 is an alpha release:" in (root / "README.md").read_text()


@pytest.mark.parametrize(
    ("model", "lock", "readme"),
    [
        (MODEL.replace("drivine-python", "drivine4j"), POETRY_LOCK, README),
        (MODEL, POETRY_LOCK.replace('name = "drivine"\n', 'name = "drivine-core"\n'), README),
        (MODEL, POETRY_LOCK, README.replace("is an alpha release:", "is a release:")),
        (MODEL, POETRY_LOCK, README + "\n1.0 is an alpha release: again.\n"),
    ],
    ids=["model file of another library", "lock without drivine", "README reworded", "README says it twice"],
)
def test_references_fails_on_a_reference_not_found_once_and_writes_nothing(
    tmp_path: Path, model: str, lock: str, readme: str
) -> None:
    root = a_project(tmp_path, model, lock, readme)
    with pytest.raises(ValueError, match="should name the version once"):
        release_version.set_references(root, release_version.Version("0.3.0"))
    assert (root / "sample" / ".drivine" / "model.json").read_text() == model
    assert (root / "sample" / "poetry.lock").read_text() == lock
    assert (root / "README.md").read_text() == readme


def test_references_fails_on_a_missing_file(tmp_path: Path) -> None:
    root = a_project(tmp_path)
    (root / "sample" / "poetry.lock").unlink()
    with pytest.raises(ValueError, match="no such file"):
        release_version.set_references(root, release_version.Version("0.3.0"))
    assert (root / "README.md").read_text() == README


def test_the_repository_s_references_name_pyproject_s_version(tmp_path: Path) -> None:
    # Rewriting the repository's own files to the version they should have already changes nothing: every reference
    # is found, and each names pyproject.toml's version. A file moved or reworded fails here, before a release does.
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    for reference in release_version.REFERENCES:
        copy = tmp_path / reference.path
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_text((ROOT / reference.path).read_text())
    release_version.set_references(tmp_path, release_version.Version(version))
    for reference in release_version.REFERENCES:
        assert (tmp_path / reference.path).read_text() == (ROOT / reference.path).read_text(), reference.path


def test_every_model_file_in_the_repository_is_a_reference() -> None:
    found: list[str] = []
    for directory, subdirectories, files in os.walk(ROOT):
        subdirectories[:] = [it for it in subdirectories if it == ".drivine" or not it.startswith((".", "__"))]
        if Path(directory).name == ".drivine" and "model.json" in files:
            found.append((Path(directory) / "model.json").relative_to(ROOT).as_posix())
    assert found
    assert set(found) <= {reference.path for reference in release_version.REFERENCES}


@pytest.mark.parametrize(
    "version",
    [
        "0.3.0",
        "0.3.0-rc.1",
        "0.3.0-rc1",
        "0.3.0-rc",
        "0.3.0-RC.01",
        "0.3.0-alpha.2",
        "0.3.0-beta.3",
        "0.3.0-a.1",
        "0.3.0-c.4",
        "0.3.0-pre.5",
        "0.3.0-preview.6",
        "0.3.0-dev.7",
        "0.3.0-post.1",
        "0.3.0-1",
        "0.3.0-rc.1.dev.2",
        "0.3.0-rc.1.post.2",
    ],
)
def test_pep440_normalizes_as_packaging_does(version: str) -> None:
    assert release_version.pep440(release_version.Version(version)) == str(packaging.version.Version(version))


def test_pep440_refuses_a_suffix_it_doesnt_read() -> None:
    with pytest.raises(ValueError, match="PEP 440"):
        release_version.pep440(release_version.Version("0.3.0-rc.1.fix"))
