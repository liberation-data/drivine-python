"""tests/shards.py: CI's parts of a run, each module whole in one part, balanced by the time each took."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests import shards
from tests.shards import parse, parts


def test_each_module_goes_into_exactly_one_part_the_longest_first_into_the_part_with_least() -> None:
    counts = {"a.py": 10, "b.py": 10, "c.py": 10, "d.py": 10}
    seconds = {"a.py": 100.0, "b.py": 60.0, "c.py": 50.0, "d.py": 10.0}

    # a.py's 100 s first, then b.py and c.py together, 110, and d.py with a.py: 110 and 110.
    assert parts(counts, seconds, 2) == [{"a.py", "d.py"}, {"b.py", "c.py"}]


def test_a_module_without_a_time_is_reckoned_at_the_average_test_of_those_with_one() -> None:
    # 2 s a test in a.py, so b.py's 30 tests are reckoned at 60 s, more than a.py's 20.
    assert parts({"a.py": 10, "b.py": 30, "c.py": 1}, {"a.py": 20.0, "c.py": 2.0}, 2) == [{"b.py"}, {"a.py", "c.py"}]


def test_the_parts_of_one_selection_are_the_same_whichever_order_it_was_collected_in() -> None:
    counts = {f"m{it}.py": it % 7 + 1 for it in range(40)}
    reversed_counts = dict(reversed(list(counts.items())))

    split = parts(counts, {}, 3)
    assert split == parts(reversed_counts, {}, 3)
    assert set().union(*split) == set(counts) and sum(len(it) for it in split) == len(counts)


@pytest.mark.parametrize("text", ["0/3", "4/3", "1", "a/b", "1/0"])
def test_a_part_is_i_of_n_numbered_from_1(text: str) -> None:
    with pytest.raises(pytest.UsageError, match="--shard takes i/n"):
        parse(text)


def test_parses_i_of_n() -> None:
    assert parse("2/3") == (2, 3)


def test_reads_the_oracle_durations_with_oracle_tests_and_the_default_ones_without(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    durations = tmp_path / "durations.json"
    durations.write_text(json.dumps({"default": {"a.py": 1.0}, "oracle": {"b.py": 2.0}}))
    monkeypatch.setattr(shards, "DURATIONS", durations)

    monkeypatch.delenv("ORACLE_TESTS", raising=False)
    assert shards.recorded() == {"a.py": 1.0}
    monkeypatch.setenv("ORACLE_TESTS", "true")
    assert shards.recorded() == {"b.py": 2.0}
    monkeypatch.setattr(shards, "DURATIONS", tmp_path / "none.json")
    assert shards.recorded() == {}


def collected(*nodeids: str) -> list[Any]:
    return [SimpleNamespace(nodeid=it) for it in nodeids]


def config(options: dict[str, str | None], deselected: list[Any]) -> Any:
    hook = SimpleNamespace(pytest_deselected=lambda items: deselected.extend(items))
    return SimpleNamespace(getoption=lambda name: options.get(name), hook=hook)


def test_each_part_keeps_its_modules_whole_and_reports_the_rest_deselected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shards, "recorded", lambda: {"a.py": 100.0, "b.py": 10.0})
    picked = collected("a.py::one", "b.py::one", "a.py::two", "b.py::two")
    kept: list[list[str]] = []
    for part in ("1/2", "2/2"):
        items, deselected = list(picked), []
        shards.pytest_collection_modifyitems(config({"--shard": part}, deselected), items)
        kept.append([it.nodeid for it in items])
        assert sorted(it.nodeid for it in items + deselected) == sorted(it.nodeid for it in picked)

    assert kept == [["a.py::one", "a.py::two"], ["b.py::one", "b.py::two"]]


def test_without_a_part_every_test_is_kept() -> None:
    items, deselected = collected("a.py::one", "b.py::one"), []
    shards.pytest_collection_modifyitems(config({}, deselected), items)
    assert len(items) == 2 and deselected == []


def test_records_the_seconds_each_module_took_setup_and_teardown_with_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(shards, "TOOK", shards.defaultdict(float))
    for nodeid, duration in [("b.py::one", 1.0), ("a.py::one", 0.25), ("b.py::one", 0.5), ("b.py::two", 2.004)]:
        shards.pytest_runtest_logreport(SimpleNamespace(nodeid=nodeid, duration=duration))
    path = tmp_path / "took.json"
    shards.pytest_sessionfinish(SimpleNamespace(config=config({"--record-durations": str(path)}, [])))

    assert json.loads(path.read_text()) == {"a.py": 0.25, "b.py": 3.5}
