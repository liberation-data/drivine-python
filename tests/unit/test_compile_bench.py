"""scripts/compile_bench.py: the parts of the bench that aren't timing, its statistics, its table and its corpus."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from tests.cypher.cypher_spec import load

_path = Path(__file__).parents[2] / "scripts" / "compile_bench.py"
_spec = importlib.util.spec_from_file_location("compile_bench", _path)
assert _spec is not None and _spec.loader is not None
compile_bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(compile_bench)


def test_p99_is_the_nearest_rank():
    hundred = compile_bench.summary([float(it) for it in range(1, 101)])
    assert hundred == {"median": 50.5, "mean": 50.5, "p99": 99.0, "max": 100.0}
    assert compile_bench.summary([float(it) for it in range(1, 1001)])["p99"] == 990.0
    assert compile_bench.summary([3.0, 1.0, 2.0])["p99"] == 3.0
    assert compile_bench.summary([7.0]) == {"median": 7.0, "mean": 7.0, "p99": 7.0, "max": 7.0}


def test_the_corpus_is_every_spec_file_but_the_ddl():
    spec_files = {it.name for it in (Path(__file__).parents[1] / "cypher").glob("*.yaml")}
    assert set(compile_bench.CORPUS) == spec_files - {"schema.yaml"}


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
    timing = {"median": 1.0, "mean": 2.0, "p99": 3.0, "max": 4.0}
    results = {
        "python": "3.13.0",
        "machine": "Linux x86_64",
        "runs": 5,
        "import": timing,
        "first_compile": dict.fromkeys(compile_bench.STORES, timing),
        "warm": {
            store: {"statements": 10, "refused": 2, "uncached": timing, "cached": timing}
            for store in compile_bench.STORES
        },
    }
    lines = compile_bench.report(results).splitlines()
    assert lines[0] == "Compile bench: Python 3.13.0 on Linux x86_64, 5 runs each, in milliseconds."
    assert "- import drivine: median 1.0, max 4.0" in lines
    for store in compile_bench.STORES:
        assert f"| {store} | 1.0 | 10 (2) | 1.0 / 2.0 / 3.0 | 1.0 / 2.0 / 3.0 |" in lines
