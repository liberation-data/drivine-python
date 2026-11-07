"""
How a term written again at once is counted on each SQL store (drivine4j#607): Postgres and SQLite hold to the character
after it without taking it, and Oracle, whose expressions don't look ahead, counts it ending where its last word does,
less those a word goes on from; or, where an occurrence may begin inside another, in the text with each character
between words written twice. Each case is counted both ways, through the expressions SQLite reads, as drivine4j's
TextQueryTest counts them through Java's.
"""

import pytest

from drivine.backend.sql.text_query import SEPARATOR, TextQuery, counted, looking_ahead
from drivine.backend.sqlite.sqlite_patterns import Patterns

_PATTERNS = Patterns()


def count(pattern: str, text: str) -> int:
    """How often the expression matches in the text, each match after the last one ends, as the stores count."""
    return sum(1 for _ in _PATTERNS.of(pattern).finditer(text))


def term(query: str) -> str:
    (scored,) = TextQuery.parse(query).scored
    return scored


def by_lookahead(query: str, text: str) -> int:
    """How often the term occurs in the text, counted as Postgres and SQLite count it."""
    return count(looking_ahead(term(query)), text)


def by_oracle(query: str, text: str) -> int:
    """How often the term occurs in the text, counted as Oracle counts it (oracle_search._oracle_count)."""
    pattern = term(query)
    two = counted(pattern)
    if two is None:
        return count(pattern, _PATTERNS.of(SEPARATOR).sub(r"\1\1", text))
    ended, continued = two
    return count(ended, text) - (0 if continued is None else count(continued, text))


def both(query: str, text: str, n: int) -> None:
    assert (by_lookahead(query, text), by_oracle(query, text)) == (n, n), f"{query} in {text!r}"


def test_a_terms_trailing_boundary_is_looked_ahead_to_and_a_prefix_term_which_has_none_is_as_it_was() -> None:
    assert looking_ahead(term("kiwi")) == "(^|[^[:alnum:]_])kiwi(?=$|[^[:alnum:]_])"
    assert looking_ahead(term("kiw*")) == "(^|[^[:alnum:]_])kiw"


@pytest.mark.parametrize(
    ("text", "n"),
    [
        ("kiwi kiwi kiwi", 3),
        ("kiwi,kiwi.kiwi!", 3),
        ("(kiwi)", 1),
        ("kiwi", 1),
        ("kiwis kiwi_kiwi kiwi2 akiwi", 0),
        ("kiwi\nkiwi\n", 2),
    ],
)
def test_a_term_written_again_at_once_is_counted_each_time_wherever_it_stands(text: str, n: int) -> None:
    both("kiwi", text, n)


@pytest.mark.parametrize(
    ("text", "n"),
    [
        ("of soup of soup", 2),
        ("of  soup, of -- soup.", 2),
        ("of soups of soup", 1),
        ("of soup_ of soup", 1),
        ("of soup_of soup", 0),
    ],
)
def test_a_phrase_written_again_at_once_is_counted_each_time_its_gap_any_punctuation(text: str, n: int) -> None:
    both('"of soup"', text, n)


def test_a_prefix_term_counts_each_word_it_starts() -> None:
    both("kiw*", "kiwi kiwis kiw akiwi", 3)
    both('"of sou*"', "of soup of soups", 2)


def test_a_word_in_any_script_is_a_word() -> None:
    both("café", "café café cafés caféé", 2)
    both("кот", "кот кот котик", 2)


def test_oracle_counts_a_term_of_words_each_once_by_two_counts_and_a_phrase_that_repeats_a_word_otherwise() -> None:
    assert counted(term("kiwi")) == ("(^|[^[:alnum:]_])kiwi", "(^|[^[:alnum:]_])kiwi[[:alnum:]_]")
    assert counted(term("kiw*")) == ("(^|[^[:alnum:]_])kiw", None)
    assert counted(term('"la la"')) is None
    assert counted(term('"la di la"')) is None
    assert (counted(term('"of soup"')) or ("",))[0] == "(^|[^[:alnum:]_])of[^[:alnum:]_]+soup"


def test_a_phrase_whose_words_begin_it_again_or_whose_last_word_starts_its_first_counts_the_same_on_oracle() -> None:
    # Two counts took different occurrences: "la la" of "la land" going on, the first ending, so counted none.
    both('"la la"', "la la land", 1)
    assert counted(term('"kiwis kiwi"')) is None
    both('"kiwis kiwi"', "kiwis kiwis kiwi", 1)
    assert by_oracle('"kiwis bird kiwi"', "kiwis bird kiwis bird kiwi") == 1


@pytest.mark.parametrize(
    ("query", "text"),
    [
        ('"kiwi kiwis"', "kiwi kiwi kiwis kiwis kiwi kiwisx kiwi kiwis"),
        ('"ki kiwi bird"', "ki ki kiwi bird ki kiwi birds ki kiwi bird ki kiwi bird"),
        ('"a1 a12"', "a1 a1 a12 a1 a12 a1 a123 a1 a12_"),
        ('"_a a_b"', "_a _a a_b _a a_b _a a_bc"),
        ("a_1", "a_1 a_1 a_1a a_1_ a_1"),
    ],
)
def test_a_phrase_of_distinct_words_whose_last_does_not_start_its_first_counts_alike_by_two_counts(
    query: str, text: str
) -> None:
    assert counted(term(query)) is not None
    assert by_oracle(query, text) == by_lookahead(query, text)


def test_a_phrase_whose_first_word_starts_a_later_one_counts_by_how_often_it_occurs() -> None:
    both('"kiwi kiwis"', "kiwi kiwi kiwis kiwis kiwi kiwisx kiwi kiwis", 2)
    both('"ki kiwi bird"', "ki ki kiwi bird ki kiwi birds ki kiwi bird ki kiwi bird", 3)


def test_oracle_counts_a_term_holding_a_number_that_is_not_a_digit_by_its_one_expression() -> None:
    # "²" is a word's to the query, not to [[:alnum:]]: "b²b" in "b²b²bx" begins inside the first.
    assert counted(term("b²b")) == (term("b²b"), None)
    both("b²b", "b²b²bx", 1)
    assert counted(term("bⅻb")) == (term("bⅻb"), None)
