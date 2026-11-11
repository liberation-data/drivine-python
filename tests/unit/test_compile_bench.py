"""scripts/compile_bench.py: the parts of the bench that aren't timing, its statistics, its table and its corpus."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.cypher.cypher_spec import load

_path = Path(__file__).parents[2] / "scripts" / "compile_bench.py"
_spec = importlib.util.spec_from_file_location("compile_bench", _path)
assert _spec is not None and _spec.loader is not None
compile_bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(compile_bench)


def test_p99_is_the_nearest_rank():
    hundred = compile_bench.summary([float(it) for it in range(1, 101)])
    assert hundred == {"min": 1.0, "median": 50.5, "mean": 50.5, "p99": 99.0, "max": 100.0}
    assert compile_bench.summary([float(it) for it in range(1, 1001)])["p99"] == 990.0
    assert compile_bench.summary([3.0, 1.0, 2.0])["p99"] == 3.0
    assert compile_bench.summary([7.0]) == {"min": 7.0, "median": 7.0, "mean": 7.0, "p99": 7.0, "max": 7.0}


def test_the_corpus_is_every_spec_file_but_the_ddl_and_the_hosts():
    spec_files = {it.name for it in (Path(__file__).parents[1] / "cypher").glob("*.yaml")}
    # host.yaml holds no Cypher: what the host's functions and row reading do.
    assert set(compile_bench.CORPUS) == spec_files - {"schema.yaml", "host.yaml"}


def test_the_corpus_takes_each_step_on_the_stores_it_runs_on():
    steps = compile_bench.corpus()
    for store in compile_bench.STORES:
        expected = [
            step.cypher
            for file in compile_bench.CORPUS
            for case in load(file).cases
            for step in case.steps
            if step.runs_on(store.lower())
        ]
        assert [cypher for it, cypher, _ in steps if it == store] == expected
    assert {store for store, _, _ in steps} == set(compile_bench.STORES)


def test_the_report_has_a_row_per_store():
    timing = {"min": 0.5, "median": 1.0, "mean": 2.0, "p99": 3.0, "max": 4.0}
    results = {
        "python": "3.13.0",
        "machine": "Linux x86_64",
        "cpu": "Example CPU",
        "runs": 5,
        "import": timing,
        "import_against": {"tree": "../main-tree", **timing},
        "first_compile": dict.fromkeys(compile_bench.STORES, timing),
        "warm": {
            store: {"statements": 10, "refused": 2, "uncached": timing, "cached": timing}
            for store in compile_bench.STORES
        },
    }
    lines = compile_bench.report(results).splitlines()
    assert lines[0] == "Compile bench: Python 3.13.0 on Linux x86_64 (Example CPU), 5 runs each, in milliseconds."
    assert "- import drivine: min 0.5, median 1.0, max 4.0" in lines
    assert "- import drivine in ../main-tree, in turn: min 0.5, median 1.0, max 4.0" in lines
    for store in compile_bench.STORES:
        assert f"| {store} | 1.0 | 10 (2) | 1.0 / 2.0 / 3.0 | 1.0 / 2.0 / 3.0 |" in lines


def test_no_runs_is_refused_before_the_bench(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setattr(sys, "argv", ["compile_bench.py", "--runs", "0"])
    with pytest.raises(SystemExit) as exit_:
        compile_bench.main()
    assert exit_.value.code == 2
    assert "--runs takes 1 or more" in capsys.readouterr().err


def test_a_fresh_run_that_fails_says_why(capfd: pytest.CaptureFixture[str]):
    with pytest.raises(subprocess.CalledProcessError):
        compile_bench._fresh("raise SystemExit('the reason it failed')", 1)
    assert "the reason it failed" in capfd.readouterr().err


def _results(least: float, first: float = 1.0, median: float = 1.0, p99: float = 1.0) -> dict[str, Any]:
    warm = {"uncached": {"median": median, "p99": p99}, "cached": {"median": 0.0, "p99": 0.0}}
    return {
        "import": {"min": least, "median": least * 2},
        "first_compile": {store: {"median": first} for store in compile_bench.STORES},
        "warm": {store: warm for store in compile_bench.STORES},
    }


def test_the_gate_fails_a_compile_worse_than_the_baseline_past_the_noise_alone_and_takes_no_import_without_a_tree():
    baseline = _results(100.0, 100.0, 1.0, 10.0)
    assert compile_bench.regressions(baseline, _results(110.0, 50.0, 1.1, 11.0)) == []
    # Without a tree to time it in turn with, import isn't gated: the baseline's is for reference only.
    worse = compile_bench.regressions(baseline, _results(500.0, 100.0, 1.0, 12.0))
    assert [it.split(":")[0] for it in worse] == [f"{store} uncached compile, p99" for store in compile_bench.STORES]


def test_the_gate_holds_import_to_the_tree_it_was_timed_in_turn_with():
    baseline = _results(100.0)
    against = {"tree": "../main-tree", "min": 190.0, "median": 200.0, "max": 300.0}
    # A runner slower all through slows both trees alike, and passes.
    assert compile_bench.regressions(baseline, _results(205.0) | {"import_against": against}) == []
    worse = compile_bench.regressions(baseline, _results(210.0) | {"import_against": against})
    assert worse == ["import drivine, min against ../main-tree: 210.0 ms against 190.0 ms (+11%)"]


def test_the_trees_imports_are_taken_in_turn_each_in_its_own_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").touch()
    calls: list[tuple[Path, str]] = []

    def fresh(code: str, runs: int, root: Path = compile_bench.ROOT, python: str = sys.executable) -> list[float]:
        calls.append((root, python))
        return [1.0 if root == tmp_path else 2.0] * runs

    monkeypatch.setattr(compile_bench, "_fresh", fresh)
    ours, theirs = compile_bench.paired_import_ms(3, tmp_path)
    assert (ours, theirs) == ([2.0] * 3, [1.0] * 3)
    other = (tmp_path, str(tmp_path / ".venv" / "bin" / "python"))
    assert calls == [other, (compile_bench.ROOT, sys.executable)] * 3


def test_a_tree_without_an_environment_is_refused(tmp_path: Path):
    with pytest.raises(SystemExit, match="has no .venv"):
        compile_bench.tree_python(tmp_path)


@pytest.mark.parametrize(("runs", "timed"), [(1, compile_bench.IMPORT_RUNS), (5, 10)])
def test_import_takes_at_least_import_runs(monkeypatch: pytest.MonkeyPatch, runs: int, timed: int):
    counts: list[int] = []

    def imports(runs: int) -> list[float]:
        counts.append(runs)
        return [1.0] * runs

    def first(store: str, runs: int) -> list[float]:
        return [1.0] * runs

    def warm(runs: int) -> dict[str, Any]:
        timing = compile_bench.summary([1.0])
        return {
            store: {"statements": 1, "refused": 0, "uncached": timing, "cached": timing}
            for store in compile_bench.STORES
        }

    monkeypatch.setattr(compile_bench, "import_ms", imports)
    monkeypatch.setattr(compile_bench, "first_compile_ms", first)
    monkeypatch.setattr(compile_bench, "warm_ms", warm)
    monkeypatch.setattr(sys, "argv", ["compile_bench.py", "--runs", str(runs)])
    assert compile_bench.main() == 0
    assert counts == [timed]
