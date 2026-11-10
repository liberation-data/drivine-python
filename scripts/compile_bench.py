"""
How long drivine takes to import and to compile Cypher to SQL: the bar the shared compiler is held to (#467, #471).

    python scripts/compile_bench.py [--json results.json] [--runs N]

It measures, each in a fresh interpreter where a fresh one is what a user's program starts with:

- import: `import drivine`, which doesn't load the compiler (that is loaded on the first compile for a SQL store);
- first compile: a process's first compile for each SQL store, the compiler's own imports and all;
- warm compile, per store, over every step of the Cypher spec (tests/cypher) that runs on the store: each statement
  compiled with the parse cache cleared first (uncached), and again with it kept (cached). Each statement's time is the
  median of its runs, and the median, mean and p99 are over the statements. A statement the compiler refuses is timed
  as well, as a refusal is a compile too, and counted apart.

The numbers are wall-clock and depend on the machine, so compare runs of one machine only: CI's, for the gate.
"""

from __future__ import annotations

import argparse
import json
import logging
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

STORES = ("POSTGRES", "SQLITE", "ORACLE")
"""The stores Cypher is compiled to SQL for."""

GRAPH = "bench"

CORPUS = ("queries.yaml", "writes.yaml", "temporal.yaml", "refusals.yaml")
"""The spec files of Cypher statements. schema.yaml is DDL, which isn't compiled."""

FIRST = "MATCH (p:Person {name: $name})-[:KNOWS]->(f) RETURN f.name AS name ORDER BY name LIMIT 10"
"""The statement a first compile is timed on: one an application would run."""


def _fresh(code: str, runs: int) -> list[float]:
    """The milliseconds `code` prints, each run in an interpreter of its own (-I: no user site, no cwd on sys.path)."""
    samples: list[float] = []
    for _ in range(runs):
        out = subprocess.run(
            [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(ROOT)!r})\n{code}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        samples.append(float(out.strip().splitlines()[-1]))
    return samples


def import_ms(runs: int) -> list[float]:
    return _fresh("import time\nt = time.perf_counter()\nimport drivine\nprint((time.perf_counter() - t) * 1000)", runs)


def first_compile_ms(store: str, runs: int) -> list[float]:
    return _fresh(
        "import time\nimport drivine\n"
        "from drivine.cypher.cypher_specification import cypher_compiled\n"
        "from drivine.query.query_specification import QuerySpecification\n"
        "from drivine.query.statement import Statement\n"
        f"spec = QuerySpecification().with_statement(Statement({FIRST!r}, 'CYPHER')).bind({{'name': 'Ann'}})\n"
        "t = time.perf_counter()\n"
        f"_, compiled = cypher_compiled(spec, {store!r}, {GRAPH!r})\n"
        "ms = (time.perf_counter() - t) * 1000\n"
        "assert compiled is not None, 'nothing was compiled'\n"
        "print(ms)",
        runs,
    )


def corpus() -> list[tuple[str, str, dict[str, Any]]]:
    """Each spec step as (store, cypher, parameters), for each store it runs on."""
    from tests.cypher.cypher_spec import expanded, load

    steps: list[tuple[str, str, dict[str, Any]]] = []
    for file in CORPUS:
        for case in load(file).cases:
            for step in case.steps:
                for store in STORES:
                    if step.runs_on(store.lower()):
                        steps.append((store, step.cypher, expanded(step.parameters)))
    return steps


def warm_ms(runs: int) -> dict[str, Any]:
    # The compiler's warnings (a RETURN of several items) would be logged thousands of times, and logging them would be
    # timed too: the bench measures the compile, not the handler an application gives its log.
    logging.getLogger("drivine").setLevel(logging.ERROR)
    from drivine.cypher.cypher_reader import read_cypher
    from drivine.cypher.cypher_specification import cypher_compiled
    from drivine.query.query_specification import QuerySpecification
    from drivine.query.statement import Statement

    def once(store: str, cypher: str, parameters: dict[str, Any]) -> tuple[float, bool]:
        spec = QuerySpecification[Any]().with_statement(Statement(cypher, "CYPHER")).bind(parameters)
        t = time.perf_counter()
        try:
            _, compiled = cypher_compiled(spec, store, GRAPH)  # pyright: ignore[reportArgumentType]
        except Exception:  # noqa: BLE001 (a refusal is timed as a compile is)
            return (time.perf_counter() - t) * 1000, True
        ms = (time.perf_counter() - t) * 1000
        if compiled is None:  # A bench of nothing compiled would pass any bar: it's a broken bench.
            raise RuntimeError(f"{store}: nothing was compiled of {cypher!r}")
        return ms, False

    steps = corpus()
    for store in STORES:  # The compiler's modules, loaded before anything is timed.
        once(store, FIRST, {"name": "Ann"})
    result: dict[str, Any] = {}
    for store in STORES:
        mine = [it for it in steps if it[0] == store]
        uncached: list[float] = []
        cached: list[float] = []
        refusals = 0
        for _, cypher, parameters in mine:
            times: list[float] = []
            refused = False
            for _ in range(runs):
                read_cypher.cache_clear()
                ms, refused = once(store, cypher, parameters)
                times.append(ms)
            uncached.append(statistics.median(times))
            refusals += refused
            cached.append(statistics.median([once(store, cypher, parameters)[0] for _ in range(runs)]))
        result[store] = {
            "statements": len(mine),
            "refused": refusals,
            "uncached": summary(uncached),
            "cached": summary(cached),
        }
    return result


def summary(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "median": round(statistics.median(ordered), 3),
        "mean": round(statistics.fmean(ordered), 3),
        "p99": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))], 3),
        "max": round(ordered[-1], 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").strip().split("\n\n")[0])
    parser.add_argument("--json", type=Path, help="write the results here as well")
    parser.add_argument("--runs", type=int, default=5, help="runs of each measurement (default 5)")
    args = parser.parse_args()
    results = {
        "python": platform.python_version(),
        "machine": f"{platform.system()} {platform.machine()}",
        "runs": args.runs,
        "import": summary(import_ms(args.runs * 2)),
        "first_compile": {store: summary(first_compile_ms(store, args.runs)) for store in STORES},
        "warm": warm_ms(args.runs),
    }
    print(report(results))
    if args.json:
        args.json.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    return 0


def report(results: dict[str, Any]) -> str:
    """The results as a Markdown table, for a run's log and its summary."""
    lines = [
        f"Compile bench: Python {results['python']} on {results['machine']}, {results['runs']} runs each, "
        "in milliseconds.",
        "",
        f"- import drivine: median {results['import']['median']}, max {results['import']['max']}",
        "",
        "| Store | First compile | Statements (refused) | Uncached median / mean / p99 | Cached median / mean / p99 |",
        "|---|---|---|---|---|",
    ]
    for store in STORES:
        w = results["warm"][store]
        u, c = w["uncached"], w["cached"]
        lines.append(
            f"| {store} | {results['first_compile'][store]['median']} | {w['statements']} ({w['refused']}) "
            f"| {u['median']} / {u['mean']} / {u['p99']} | {c['median']} / {c['mean']} / {c['p99']} |"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
