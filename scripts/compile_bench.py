"""
How long drivine takes to import and to compile Cypher to SQL: the bar the shared compiler is held to (#467, #471).

    python scripts/compile_bench.py [--json results.json] [--runs N] [--against baseline.json] [--import-against tree]

It measures, each in a fresh interpreter where a fresh one is what a user's program starts with:

- import: `import drivine`, which doesn't load the compiler (that is loaded on the first compile for a SQL store);
- first compile: a process's first compile for each SQL store, the compiler's own imports and all;
- warm compile, per store, over every step of the Cypher spec (tests/cypher) that runs on the store: each statement
  compiled with the parse cache cleared first (uncached), and again with it kept (cached). Each statement's time is the
  median of its runs, and the median, mean and p99 are over the statements. A statement the compiler refuses is timed
  as well, as a refusal is a compile too, and counted apart. The shared compiler keeps no parse cache, so the two
  differ only by noise: "uncached" clears the parse cache of the compiler Drivine had before (`read_cypher`), which the
  baseline (tests/bench/compile-baseline.json) was measured on, and which no compile calls now.

With `--against`, it fails where a measure is worse than its bar by more than NOISE (see regressions): a compile's is
the baseline's, and import's is that of the checkout `--import-against` names, timed in turn with this tree's.

The numbers are wall-clock and depend on the machine, so compare runs of one machine only: CI's, for the gate.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
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


def _fresh(code: str, runs: int, root: Path = ROOT, python: str = sys.executable) -> list[float]:
    """
    The milliseconds `code` prints, each run in an interpreter of its own (-I: no user site, no cwd on sys.path), with
    `root` first on sys.path. Its stderr is the bench's, so a run that fails says why in the log.
    """
    samples: list[float] = []
    for _ in range(runs):
        out = subprocess.run(
            [python, "-I", "-c", f"import sys; sys.path.insert(0, {str(root)!r})\n{code}"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout
        samples.append(float(out.strip().splitlines()[-1]))
    return samples


IMPORT = "import time\nt = time.perf_counter()\nimport drivine\nprint((time.perf_counter() - t) * 1000)"
"""What an import is timed by: `import drivine` alone, which doesn't load the compiler."""


def import_ms(runs: int) -> list[float]:
    return _fresh(IMPORT, runs)


def tree_python(tree: Path) -> str:
    """The interpreter of another checkout's own environment, as `uv sync` makes it there."""
    for candidate in (tree / ".venv" / "bin" / "python", tree / ".venv" / "Scripts" / "python.exe"):
        if candidate.exists():
            return str(candidate)
    raise SystemExit(f"{tree} has no .venv: run `uv sync` there first")


def paired_import_ms(runs: int, other: Path) -> tuple[list[float], list[float]]:
    """
    This tree's imports and `other`'s, each in its own environment, taken in turn on the one machine: so a runner that
    is slower all through (a CI runner's CPU type) slows both alike, and the gate compares the code, not the runner.
    """
    python = tree_python(other)
    ours: list[float] = []
    theirs: list[float] = []
    for _ in range(runs):
        theirs += _fresh(IMPORT, 1, other, python)
        ours += _fresh(IMPORT, 1)
    return ours, theirs


def cpu_model() -> str:
    """The machine's CPU, for the record: CI runners of one label differ, and their speed with them."""
    try:
        if platform.system() == "Linux":
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
        if platform.system() == "Darwin":
            return subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"], check=True, stdout=subprocess.PIPE, text=True
            ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        pass
    return platform.processor() or "unknown"


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
    from drivine.drivine_error import DrivineError
    from drivine.query.query_specification import QuerySpecification
    from drivine.query.statement import Statement

    def once(store: str, cypher: str, parameters: dict[str, Any]) -> tuple[float, bool]:
        spec = QuerySpecification[Any]().with_statement(Statement(cypher, "CYPHER")).bind(parameters)
        t = time.perf_counter()
        try:
            _, compiled = cypher_compiled(spec, store, GRAPH)  # pyright: ignore[reportArgumentType]
        except DrivineError:  # A refusal is timed as a compile is. Anything else is a crash, which fails the bench.
            return (time.perf_counter() - t) * 1000, True
        ms = (time.perf_counter() - t) * 1000
        if compiled is None:  # A bench of nothing compiled would pass any bar: it's a broken bench.
            raise RuntimeError(f"{store}: nothing was compiled of {cypher!r}")
        return ms, False

    steps = corpus()
    for store in STORES:  # The compiler's modules, loaded before anything is timed.
        once(store, FIRST, {"name": "Ann"})
    # ANTLR's prediction cache, warmed on the whole corpus before anything is timed: else the first store timed would
    # pay for warming it, and with few runs its numbers would be that, not its compile.
    for cypher in dict.fromkeys(cypher for _, cypher, _ in steps):
        try:
            read_cypher(cypher)
        except DrivineError:
            pass
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
    """The samples' min, median, mean, p99 (by nearest rank: the least sample at or above 99% of them) and max."""
    ordered = sorted(samples)
    return {
        "min": round(ordered[0], 3),
        "median": round(statistics.median(ordered), 3),
        "mean": round(statistics.fmean(ordered), 3),
        "p99": round(ordered[math.ceil(len(ordered) * 0.99) - 1], 3),
        "max": round(ordered[-1], 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").strip().split("\n\n")[0])
    parser.add_argument("--json", type=Path, help="write the results here as well")
    parser.add_argument("--runs", type=int, default=5, help="runs of each measurement (default 5)")
    parser.add_argument(
        "--import-against",
        type=Path,
        help=(
            "another checkout, `uv sync`ed (CI's: main): time its imports in turn with this tree's, and, with "
            "--against, gate on them"
        ),
    )
    parser.add_argument(
        "--against",
        type=Path,
        help="a baseline's results (as --json writes them): fail where a measure the gate takes is worse past NOISE",
    )
    args = parser.parse_args()
    if args.runs < 1:  # A median of no runs is none: say so before the bench, not as a StatisticsError after it.
        parser.error("--runs takes 1 or more")
    imports = max(IMPORT_RUNS, args.runs * 2)
    results: dict[str, Any] = {
        "python": platform.python_version(),
        "machine": f"{platform.system()} {platform.machine()}",
        "cpu": cpu_model(),
        "runs": args.runs,
    }
    if args.import_against:
        ours, theirs = paired_import_ms(imports, args.import_against)
        results["import"] = summary(ours)
        results["import_against"] = {"tree": str(args.import_against), **summary(theirs)}
    else:
        results["import"] = summary(import_ms(imports))
    results |= {
        "first_compile": {store: summary(first_compile_ms(store, args.runs)) for store in STORES},
        "warm": warm_ms(args.runs),
    }
    print(report(results))
    if args.json:
        args.json.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    if args.against:
        baseline = json.loads(args.against.read_text(encoding="utf-8"))
        print(f"\n- import drivine, for reference only: the baseline's median {baseline['import']['median']} ms")
        worse = regressions(baseline, results)
        print("\n" + ("\n".join(f"- worse: {it}" for it in worse) or f"No measure is worse than {args.against}'s."))
        return 1 if worse else 0
    return 0


NOISE = 0.10
"""How much worse than the baseline a measure may be and still pass: a run's own noise, on CI's machines."""

IMPORT_RUNS = 7
"""
The fewest fresh imports timed, whose least the gate takes: a CI runner adds tens of milliseconds to some imports and
not others, so their median moved by a quarter between runs of one commit (193 to 199 ms on one runner, 142 to 157 on
another, #495), where their least is the import's own cost, as the code makes it.
"""


def regressions(baseline: dict[str, Any], results: dict[str, Any]) -> list[str]:
    """
    Each measure the gate takes that is worse than its bar by more than NOISE (#467, #496). `import drivine`'s least,
    over at least IMPORT_RUNS, is held to the least of the tree it was timed in turn with on the one machine
    (`import_against`, CI's main), as a runner's type moved it by a quarter where the code didn't; with no such tree it
    isn't gated, and the baseline's is printed for reference. Per store, the first compile's median and the uncached
    compile's median and p99 are held to the baseline's. The cached figures are for the record: the shared compiler
    keeps no parse of its own, so uncached is the fair comparison.
    """
    measures: list[tuple[str, float, float]] = []
    if "import_against" in results:
        against = results["import_against"]
        measures.append((f"import drivine, min against {against['tree']}", against["min"], results["import"]["min"]))
    for store in STORES:
        measures.append(
            (
                f"{store} first compile, median",
                baseline["first_compile"][store]["median"],
                results["first_compile"][store]["median"],
            )
        )
        for stat in ("median", "p99"):
            measures.append(
                (
                    f"{store} uncached compile, {stat}",
                    baseline["warm"][store]["uncached"][stat],
                    results["warm"][store]["uncached"][stat],
                )
            )
    return [
        f"{name}: {now} ms against {then} ms (+{(now / then - 1) * 100:.0f}%)"
        for name, then, now in measures
        if now > then * (1 + NOISE)
    ]


def _against_line(results: dict[str, Any]) -> list[str]:
    """The import of the tree timed in turn with this one, where one was."""
    against = results.get("import_against")
    if against is None:
        return []
    return [
        f"- import drivine in {against['tree']}, in turn: min {against['min']}, median {against['median']}, "
        f"max {against['max']}"
    ]


def report(results: dict[str, Any]) -> str:
    """The results as a Markdown table, for a run's log and its summary."""
    lines = [
        f"Compile bench: Python {results['python']} on {results['machine']} ({results.get('cpu', 'unknown')}), "
        f"{results['runs']} runs each, in milliseconds.",
        "",
        f"- import drivine: min {results['import']['min']}, median {results['import']['median']}, "
        f"max {results['import']['max']}",
        *_against_line(results),
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
