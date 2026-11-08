"""
CI's parts of a test run: `pytest --shard 2/3` runs the second of three parts of what it would otherwise run, after `-k`
and `-m` have picked it, so that the parts of one selection, run on runners of their own, hold each of its tests once.
A module goes whole into one part, its module-scoped fixtures with it, in the order it is collected in. The parts are
balanced by how long each module took in CI, as tests/durations.json records it: under "oracle" with ORACLE_TESTS=true,
which runs what Oracle adds (`-k oracle`), and under "default" without. A module it doesn't name is reckoned at the
average time of a test in those it does, for each test it holds.

`pytest --record-durations <file>` writes how long each module's tests took, setup and teardown with them, in seconds,
in the form of one of durations.json's two maps. CI uploads it from each part; merge the parts' files into
durations.json when the parts drift apart.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path

import pytest

DURATIONS = Path(__file__).with_name("durations.json")

Part = tuple[int, int]


def parse(text: str) -> Part:
    """`i/n`, the part i of n, numbered from 1."""
    index, _, count = text.partition("/")
    try:
        part = (int(index), int(count))
    except ValueError:
        part = (0, 0)
    if not 1 <= part[0] <= part[1]:
        raise pytest.UsageError(f"--shard takes i/n, the part i of n parts, numbered from 1: not {text!r}.")
    return part


def module_of(item: pytest.Item) -> str:
    return item.nodeid.split("::", 1)[0]


def recorded() -> dict[str, float]:
    """The seconds each module took, for the stores this run has."""
    if not DURATIONS.exists():
        return {}
    key = "oracle" if os.environ.get("ORACLE_TESTS") == "true" else "default"
    return json.loads(DURATIONS.read_text()).get(key, {})


def parts(counts: dict[str, int], seconds: dict[str, float], count: int) -> list[set[str]]:
    """
    The modules, by how many of their tests are to run, put in `count` parts of about the same time: the longest
    first, each into the part with least so far, ties going to the lower part and the module named first.
    """
    known = {module: seconds[module] for module in counts if module in seconds}
    tested = sum(counts[module] for module in known)
    average = sum(known.values()) / tested if tested else 1.0
    weight = {module: known.get(module, average * counts[module]) for module in counts}
    totals = [0.0] * count
    split: list[set[str]] = [set() for _ in range(count)]
    for module in sorted(counts, key=lambda it: (-weight[it], it)):
        lightest = min(range(count), key=lambda it: (totals[it], it))
        totals[lightest] += weight[module]
        split[lightest].add(module)
    return split


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("drivine CI parts")
    group.addoption("--shard", default=None, help="Run the part i of n of the tests picked, as i/n.")
    group.addoption("--record-durations", default=None, help="Write the seconds each module's tests took to a file.")


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Keeps the tests of this part, after -k and -m, whose hooks run before this one, have deselected theirs."""
    text = config.getoption("--shard")
    if not text:
        return
    index, count = parse(text)
    counts: dict[str, int] = defaultdict(int)
    for item in items:
        counts[module_of(item)] += 1
    mine = parts(counts, recorded(), count)[index - 1]
    kept = [it for it in items if module_of(it) in mine]
    left = [it for it in items if module_of(it) not in mine]
    if left:
        config.hook.pytest_deselected(items=left)
    items[:] = kept


TOOK: dict[str, float] = defaultdict(float)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    TOOK[report.nodeid.split("::", 1)[0]] += report.duration


def pytest_sessionfinish(session: pytest.Session) -> None:
    path = session.config.getoption("--record-durations")
    if path:
        Path(path).write_text(json.dumps({it: round(TOOK[it], 2) for it in sorted(TOOK)}, indent=1) + "\n")
