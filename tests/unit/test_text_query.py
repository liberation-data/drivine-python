"""drivine4j's TextQueryTest, ported as TypeScript's Drivine ported it: the same queries are read alike everywhere."""

import pytest

from drivine import DrivineError
from drivine.backend.oracle.text_query import TextQuery


def word(text: str) -> str:
    return f"(^|[^[:alnum:]_]){text}($|[^[:alnum:]_])"


def oracle_text(query: str) -> str:
    return TextQuery.parse(query).oracle_text


def test_scores_words_each_matched_whole_and_in_lower_case():
    query = TextQuery.parse("Graph databases")
    assert query.scored == [word("graph"), word("databases")]
    assert query.required == []
    assert query.excluded == []


def test_a_plus_or_and_requires_a_word_and_a_minus_or_not_excludes_one():
    query = TextQuery.parse("+kotlin graph AND store -python NOT rust")
    assert query.scored == [word("kotlin"), word("graph"), word("store")]
    assert query.required == [word("kotlin"), word("store")]
    assert query.excluded == [word("python"), word("rust")]


def test_reads_a_phrase_as_its_words_a_star_as_a_prefix_and_a_fields_word_as_the_word():
    assert TextQuery.parse('"graph data" data* name:Ada').scored == [
        "(^|[^[:alnum:]_])graph[^[:alnum:]_]+data($|[^[:alnum:]_])",
        "(^|[^[:alnum:]_])data",
        word("ada"),
    ]


def test_does_not_look_for_what_is_not_a_word():
    assert TextQuery.parse("c++ (.*)").scored == [word("c")]
    assert "." not in TextQuery.parse("a.b").scored[0]


def test_accumulates_requires_and_takes_away_for_oracle_text():
    assert oracle_text("Graph databases") == "({graph} ACCUM {databases})"
    assert (
        oracle_text("+kotlin graph AND store -python NOT rust")
        == "({kotlin} ACCUM {graph} ACCUM {store}) AND {kotlin} AND {store} NOT {python} NOT {rust}"
    )
    assert oracle_text('"graph data" data* name:Ada') == "({graph data} ACCUM data% ACCUM {ada})"
    # A word that is one of Oracle Text's own is still a word.
    assert oracle_text("near about") == "({near} ACCUM {about})"


@pytest.mark.parametrize("query", ["-only", "  ", '""', "*", 'title:""'])
def test_refuses_a_query_with_no_word_to_look_for(query):
    with pytest.raises(DrivineError, match="needs a word"):
        TextQuery.parse(query)


def test_reads_the_symbols_for_and_or_and_not_as_the_words():
    query = TextQuery.parse("graph && store || index ! python")
    assert query.scored == [word("graph"), word("store"), word("index")]
    assert query.required == [word("store")]
    assert query.excluded == [word("python")]


def test_reads_an_operator_with_nothing_after_it_as_nothing():
    query = TextQuery.parse("graph AND")
    assert query.scored == [word("graph")]
    assert query.required == []
    assert query.oracle_text == "({graph})"


def test_reads_a_field_scoped_to_a_phrase_as_the_phrase_and_to_a_word_as_the_word():
    phrase = TextQuery.parse('title:"graph data"')
    assert phrase.scored == ["(^|[^[:alnum:]_])graph[^[:alnum:]_]+data($|[^[:alnum:]_])"]
    assert phrase.oracle_text == "({graph data})"
    field = TextQuery.parse("name:Ada")
    assert field.scored == [word("ada")]
    assert field.oracle_text == "({ada})"


def test_reads_a_star_in_the_middle_of_a_word_as_a_gap_between_two_words():
    query = TextQuery.parse("foo*bar")
    assert query.scored == ["(^|[^[:alnum:]_])foo[^[:alnum:]_]+bar($|[^[:alnum:]_])"]
    assert query.oracle_text == "({foo bar})"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("+kotlin", "({kotlin}) AND {kotlin}"),
        ("kotlin -python", "({kotlin}) NOT {python}"),
        ("kotlin NOT python ! rust", "({kotlin}) NOT {python} NOT {rust}"),
        ("pre*", "(pre%)"),
        ('"graph pre*"', "({graph} pre%)"),
        # A star after a backslash that is itself text is the start of a word.
        ("pre\\\\*", "(pre%)"),
        # What stands before a backslashed colon is kept, where a bare colon makes it a field.
        ("https\\:\\/\\/example.com\\/docs", "({https example com docs})"),
        ("https://example.com/docs", "({example com docs})"),
        # A backslashed quote neither opens nor closes a phrase.
        ('say \\"hello world\\"', "({say} ACCUM {hello} ACCUM {world})"),
        ('"say \\"hello\\" now"', "({say hello now})"),
        ("graph\\ data", "({graph data})"),
        # A backslash before a letter is text, a gap between two words; "C:" is a field.
        ("foo\\bar", "({foo bar})"),
        ("DOMAIN\\user", "({domain user})"),
        ("C:\\Users\\bob", "({users bob})"),
        ("graph\\", "({graph})"),
        # A phrase only a backslashed quote can close is closed by it; the star is not the phrase's last character.
        ('"C:\\" foo', "({c} ACCUM {foo})"),
        ('"pre*\\" foo', "({pre} ACCUM {foo})"),
        # Not closed by a backslashed quote when a bare quote stands later.
        ('"C:\\" and "D:\\"', "({c and})"),
        ('"foo\\\nbar"', "({foo bar})"),
        ('"graph pre*\n"', "({graph pre})"),
    ],
)
def test_reads_oracle_text(query, expected):
    assert oracle_text(query) == expected


def test_reads_a_sign_after_a_backslash_as_text():
    query = TextQuery.parse("graph \\+kotlin \\-python")
    assert query.scored == [word("graph"), word("kotlin"), word("python")]
    assert query.required == []
    assert query.excluded == []


def test_reads_a_closing_star_after_a_backslash_as_text():
    query = TextQuery.parse("pre\\*")
    assert query.scored == [word("pre")]
    assert query.oracle_text == "({pre})"


def test_reads_an_operator_written_with_backslashes_as_text_holding_no_word():
    query = TextQuery.parse("graph \\&\\& store \\! python")
    assert query.scored == [word("graph"), word("store"), word("python")]
    assert query.required == []
    assert query.excluded == []


def test_requires_a_word_whose_punctuation_is_escaped_as_the_phrase_of_its_parts():
    query = TextQuery.parse("+PN\\-4471\\-B order")
    assert query.oracle_text == "({pn 4471 b} ACCUM {order}) AND {pn 4471 b}"
    assert len(query.required) == 1


def test_an_operator_before_a_token_with_no_word_does_not_reach_the_word_after_it():
    negated = TextQuery.parse("graph NOT \\! python")
    assert negated.scored == [word("graph"), word("python")]
    assert negated.excluded == []
    conjoined = TextQuery.parse("graph AND \\&\\& store")
    assert conjoined.scored == [word("graph"), word("store")]
    assert conjoined.required == []


def test_reads_a_word_or_a_phrase_of_any_length():
    long = "a" * 200_000
    assert oracle_text(long) == f"({{{long}}})"
    assert oracle_text(f'"{long}"') == f"({{{long}}})"
    assert oracle_text(f"title:{long}") == f"({{{long}}})"


def test_reads_a_query_of_many_phrases_that_never_close_in_one_pass():
    assert len(TextQuery.parse('"graph ' * 50_000).scored) == 50_000
