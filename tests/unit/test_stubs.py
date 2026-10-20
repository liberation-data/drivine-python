import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

import drivine
from drivine import DrivineError, Ref
from drivine.stubs.__main__ import main
from drivine.stubs.stub_writer import StubWriter

USAGE = """\
from drivine import times
from models_refs import refs

from tests.models import Customer, Movie, PersonCareer, Puppy

c = refs(PersonCareer)
fine = [
    c.person.name.eq("Ada"),
    c.person.bio.contains("Lead"),
    c.employment_history.target.name.starts_with("A"),
    c.employment_history.any(lambda it: [it.role.eq("Lead"), it.target.name.eq("Acme")]),
    c.person.instance_of(Puppy),
    c.field("person"),
    refs(Movie).released.gt(2000),
    refs(Movie).released.desc(),
    refs(Customer).metadata.source.eq("wiki"),
    refs(Customer).tiers.has_item("Gold"),
    refs(Customer).property("metadata.score").gte(3),
    refs(Puppy).age_in_weeks.lt(10),
    times(refs(Movie).released, 2),
]
refs(Movie).released.gt("2000")  # error
refs(Movie).released.contains("x")  # error
c.person.nmae  # error
times(refs(Movie).title, 2)  # error
refs(Customer).tiers.gt(1)  # error
"""


@pytest.fixture
def written(tmp_path: Path) -> Path:
    out = tmp_path / "models_refs.py"
    assert main(["tests.models", "--out", str(out)]) == 0
    return out


def test_writes_a_module_that_is_drivines_refs_at_run_time(written: Path):
    spec = importlib.util.spec_from_file_location("models_refs", written)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.refs is drivine.refs
    assert module.PersonCareerRefs is Ref and module.PersonNodes is Ref


def test_types_each_field_as_typescript_derives_it(written: Path):
    stub = written.with_suffix(".pyi").read_text()
    for line in [
        "    released: ComparableRef[int]",
        "    bio: StringRef",
        "    metadata: BagRefs",
        "    tiers: ListRef[str]",
        "    tags: ListRef[str]",
        "    preferences: ValueRef[Any]",
        "    stamp: StringRef",
        "    employment_history: WorkHistoryNodes",
        "    target: OrganizationNodes",
        "    sub_locations: LocationHierarchyNodes",
    ]:
        assert line in stub, line
    # Computed when the view is read, and transient: not what a query can test.
    assert "movie_count" not in stub and "scratch" not in stub and "note:" not in stub
    # A subclass's overload before its parent's, which would match it too; a catch-all last.
    assert stub.index("type[Puppy]") < stub.index("type[Dog]") < stub.index("type[Animal]")
    assert stub.rstrip().endswith("def refs(type_: type[object], /) -> Any: ...")


def test_check_fails_when_the_files_are_out_of_date(written: Path, capsys: pytest.CaptureFixture[str]):
    assert main(["tests.models", "--out", str(written), "--check"]) == 0
    written.with_suffix(".pyi").write_text("stale")
    assert main(["tests.models", "--out", str(written), "--check"]) == 1
    assert "Out of date" in capsys.readouterr().err


def test_refuses_a_module_with_no_models():
    with pytest.raises(DrivineError, match="declares no NodeFragment"):
        StubWriter(["json"], "python -m drivine.stubs json").write()


def test_a_type_checker_checks_each_query_against_the_models(written: Path):
    usage = written.with_name("usage.py")
    usage.write_text(USAGE)
    checked = subprocess.run(
        [
            sys.executable,
            "-m",
            "pyright",
            "--outputjson",
            "--pythonpath",
            sys.executable,
            str(usage),
            str(written.with_suffix(".pyi")),
        ],
        capture_output=True,
        text=True,
        cwd=written.parent,
    )
    report = json.loads(checked.stdout)
    errors = {
        it["range"]["start"]["line"] + 1
        for it in report["generalDiagnostics"]
        if it["severity"] == "error" and it["file"].endswith("usage.py")
    }
    expected = {i + 1 for i, line in enumerate(USAGE.splitlines()) if line.endswith("# error")}
    assert errors == expected, report["generalDiagnostics"]
    assert not [
        it for it in report["generalDiagnostics"] if not it["file"].endswith("usage.py") and it["severity"] == "error"
    ]
