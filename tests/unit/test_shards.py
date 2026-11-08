"""tests/shards.py: CI's parts of a run, each module whole in one part, balanced by the time each took."""

import pytest

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
