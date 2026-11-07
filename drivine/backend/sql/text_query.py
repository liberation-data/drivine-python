from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import cache
from typing import Literal

from drivine.backend.sqlite.sqlite_patterns import class_text, property_ranges, union
from drivine.drivine_error import DrivineError


@cache
def _gap() -> re.Pattern[str]:
    """
    What stands between two words: a run of anything but letters as Unicode's Alphabetic holds them, decimal digits and
    underscores, as a term's expression's `[[:alnum:]_]` reads them in the text, on Postgres and through sqlite_patterns
    (Java's `\\p{Alnum}`): it takes in the vowel signs of Indic scripts and the letter numbers (`ⅻ`). Read as `\\w`, a
    query split a Hindi word at its vowel sign, which the text went on from, and kept "²" in a word where the text ends
    one at it, as Neo4j's analyzer does (drivine4j#616). Oracle's `[[:alnum:]]` holds every combining mark too.
    """
    word = class_text(union(property_ranges("Alnum"), [(ord("_"), ord("_"))]), negated=True)
    # Built on the first query, not on import: the ranges are read from every code point.
    return re.compile(word + "+")


def _is_gap(char: str) -> bool:
    """Whether `char` is a character between two words (see `_gap`)."""
    return _gap().fullmatch(char) is not None


_SPACES = " \t\n\u000b\u000c\r"
"""The characters that end a term."""

_BOUNDARY_BEFORE = "(^|[^[:alnum:]_])"
_BOUNDARY_AFTER = "($|[^[:alnum:]_])"
_BOUNDARY_AHEAD = "(?=$|[^[:alnum:]_])"
"""What `_BOUNDARY_AFTER` holds to, without taking the character it is: Postgres's and SQLite's lookahead."""
_BETWEEN = "[^[:alnum:]_]+"
"""What stands between the words of a phrase in a term's expression."""

SEPARATOR = "([^[:alnum:]_])"
"""
One character between words, as a group: for a store whose expressions don't look ahead, Oracle's. Each such character
of the text written twice (`\\1\\1`), one is there for each, so a term's expression counts in it as `looking_ahead`
counts in the text. It is the slow way, a fifth as fast as `counted`'s over 100,000 texts on Oracle, kept for the terms
`counted` can't count.
"""


def looking_ahead(pattern: str) -> str:
    """
    A term's expression ending where its last word does, the character after it held to but not taken: counted, a term
    written again at once still has the character before it (drivine4j#607). Taken, "kiwi kiwi kiwi" counted twice. For
    a store whose expressions look ahead.
    """
    return pattern.removesuffix(_BOUNDARY_AFTER) + _BOUNDARY_AHEAD if pattern.endswith(_BOUNDARY_AFTER) else pattern


def counted(pattern: str) -> tuple[str, str | None] | None:
    """
    For a store whose expressions don't look ahead, Oracle's: the term's expression ending where its last word does, and
    that expression holding a letter, a digit or an underscore after it. The term occurs as many times as the first
    matches less the second, and neither takes the character before the next, so it counts as `looking_ahead` does. None
    for the second where the term is the start of a word, which may go on (drivine4j#607).

    None where the term is a phrase that repeats a word, or whose last word starts its first: there an occurrence may
    begin inside another, "la la" in "la la land" or "kiwis kiwi" in "kiwis kiwis kiwi", and the two counts take
    different ones. Such a term is counted in the text with each `SEPARATOR` written twice.

    The pattern itself, with None for the second, where a word holds a number that isn't a digit, the "²" of "b²b": a
    word's to `_GAP` but not to `[[:alnum:]]`, so an occurrence may begin at it, and neither way above counts it. The
    one count undercounts the term only where it is written again at once, as it did before.
    """
    if not pattern.endswith(_BOUNDARY_AFTER):
        return pattern, None
    ended = pattern.removesuffix(_BOUNDARY_AFTER)
    words = ended.removeprefix(_BOUNDARY_BEFORE).split(_BETWEEN)
    if any(not (char.isalpha() or char.isdecimal() or char == "_") for word in words for char in word):
        return pattern, None
    overlaps = len(set(words)) < len(words) or (len(words) > 1 and words[0].startswith(words[-1]))
    return None if overlaps else (ended, f"{ended}[[:alnum:]_]")


def is_prefix(pattern: str) -> bool:
    """Whether a term's expression is a prefix term's: one whose last word is the start of a word, `kiw*`."""
    return not pattern.endswith(_BOUNDARY_AFTER)


_OPERATORS: dict[str, Literal["MUST", "NOT", "OR"]] = {
    "AND": "MUST",
    "&&": "MUST",
    "NOT": "NOT",
    "!": "NOT",
    "OR": "OR",
    "||": "OR",
}
"""What an operator does to the token after it."""


@dataclass(frozen=True)
class _Char:
    """One character of a query, and whether a backslash before it made it text."""

    char: str
    escaped: bool


@dataclass(frozen=True)
class _Token:
    """One term as it is written: its sign, whether it stood in quotes, and its characters after any field."""

    sign: str | None
    quoted: bool
    chars: list[_Char]


@dataclass(frozen=True)
class _Term:
    """One term as Oracle is asked it: as a whole-word expression, and for Oracle Text."""

    words: list[str]
    """The term's words in turn, the last of them the start of a word for a prefix."""
    pattern: str
    oracle_text: str


@dataclass
class _Parts:
    """The parts of a query as they are read, each kind as expressions and as Oracle Text."""

    scored: list[str] = field(default_factory=lambda: [])
    scored_words: list[list[str]] = field(default_factory=lambda: [])
    required: list[str] = field(default_factory=lambda: [])
    excluded: list[str] = field(default_factory=lambda: [])
    scored_text: list[str] = field(default_factory=lambda: [])
    required_text: list[str] = field(default_factory=lambda: [])
    excluded_text: list[str] = field(default_factory=lambda: [])


@dataclass(frozen=True)
class TextQuery:
    """
    A full-text query as Oracle is asked it (drivine4j's TextQuery, read alike). Without a text index it is the terms
    that score a node, those it must hold and those it must not, each a regular expression that matches the term as a
    whole word in lower-cased text. Through an Oracle Text index it is `oracle_text`.

    It reads the plain core of the Lucene syntax the graph engines take: words, a `"quoted phrase"`, `+must`, `-not` and
    `NOT not`, a `prefix*`, and `field:word` as the word. `AND` makes the word after it one a node must hold; `OR` is
    the default between words. Anything else is read as text. A backslash before a character that is not part of a word
    makes that character text; before a letter, a digit or an underscore it is itself text.
    """

    scored: list[str]
    required: list[str]
    excluded: list[str]
    scored_words: list[list[str]]
    """
    The words of each term of `scored`, in turn: a node whose text matches the term holds each of them, so a store may
    narrow what it counts to the nodes that hold them all, by an index of their text.
    """
    oracle_text: str
    """
    The same query for an Oracle Text index: the scoring terms accumulated, each required one conjoined and each
    excluded one taken away. A word is in braces, so it is a word and never one of Oracle Text's operators.
    """

    @staticmethod
    def parse(query: str) -> TextQuery:
        """Reads the query. It must name at least one term that scores."""
        parts = _Parts()
        must = False
        not_ = False
        for token in _tokens(query):
            operator = _OPERATORS.get(_operator_of(token) or "")
            if operator in ("MUST", "NOT"):
                must = must or operator == "MUST"
                not_ = not_ or operator == "NOT"
            elif operator != "OR":
                _add(parts, token, must, not_)
                # An operator applies to the token after it and to no other, whether or not that token holds a word.
                must = False
                not_ = False
        if not parts.scored:
            raise DrivineError(f"A full-text query needs a word to look for: '{query}' has none.")
        text = " AND ".join([f"({' ACCUM '.join(parts.scored_text)})", *parts.required_text]) + "".join(
            f" NOT {it}" for it in parts.excluded_text
        )
        return TextQuery(parts.scored, parts.required, parts.excluded, parts.scored_words, text)


def _add(parts: _Parts, token: _Token, must: bool, not_: bool) -> None:
    """Adds the token's term, if it holds one, to what scores, what is required and what is excluded."""
    term = _term_of(token)
    if term is None:
        return
    if token.sign == "-" or not_:
        parts.excluded.append(term.pattern)
        parts.excluded_text.append(term.oracle_text)
        return
    if token.sign == "+" or must:
        parts.required.append(term.pattern)
        parts.required_text.append(term.oracle_text)
    parts.scored.append(term.pattern)
    parts.scored_words.append(term.words)
    parts.scored_text.append(term.oracle_text)


def _is_space(it: _Char) -> bool:
    return not it.escaped and it.char in _SPACES


def _is_bare(it: _Char, other: str) -> bool:
    """Whether the character is `other` with its meaning in the syntax, which a backslash takes away."""
    return not it.escaped and it.char == other


def _operator_of(token: _Token) -> str | None:
    """The token as written when it is a word with no backslash in it, which is what an operator is."""
    if token.quoted or any(it.escaped for it in token.chars):
        return None
    return "".join(it.char for it in token.chars)


def _chars_of(query: str) -> list[_Char]:
    """
    The characters of the query. A backslash and a character after it that is not part of a word are that one
    character, as text. Any other backslash is a character itself.
    """
    out: list[_Char] = []
    at = 0
    while at < len(query):
        following = query[at + 1] if at + 1 < len(query) else None
        if query[at] == "\\" and following is not None and _is_gap(following):
            out.append(_Char(following, True))
            at += 2
        else:
            out.append(_Char(query[at], False))
            at += 1
    return out


def _tokens(query: str) -> list[_Token]:
    """The query's tokens, read left to right: a sign, a field (read as the term), then a quoted phrase or a word."""
    chars = _chars_of(query)
    last_quote = _last_index(chars, lambda it: _is_bare(it, '"'))
    last_escaped_quote = _last_index(chars, lambda it: it.escaped and it.char == '"')

    def has_text(index: int) -> bool:
        return index < len(chars) and not _is_space(chars[index])

    out: list[_Token] = []
    at = 0
    while True:
        while at < len(chars) and _is_space(chars[at]):
            at += 1
        if at == len(chars):
            return out
        # A sign is one only when a term follows it.
        sign = chars[at].char if (_is_bare(chars[at], "+") or _is_bare(chars[at], "-")) and has_text(at + 1) else None
        if sign:
            at += 1
        at = _past_field(chars, at, has_text)
        phrase = _phrase_at(chars, at, last_quote, last_escaped_quote)
        if phrase is not None:
            out.append(_Token(sign, True, phrase[0]))
            at = phrase[1]
            continue
        start = at
        while has_text(at):
            at += 1
        out.append(_Token(sign, False, chars[start:at]))


def _past_field(chars: list[_Char], start: int, has_text: Callable[[int], bool]) -> int:
    """Where the term starts when a field stands at `start`: after the colon that ends the field."""
    colon = start
    while has_text(colon) and not _is_bare(chars[colon], ":") and not _is_bare(chars[colon], '"'):
        colon += 1
    is_field = colon > start and has_text(colon) and _is_bare(chars[colon], ":") and has_text(colon + 1)
    return colon + 1 if is_field else start


def _phrase_at(
    chars: list[_Char], open_: int, last_quote: int, last_escaped_quote: int
) -> tuple[list[_Char], int] | None:
    """
    The phrase that opens at `open_`, and where reading goes on after it; None when no quote opens one there or none
    closes it. While no bare quote closes it, the first `\\"` is read as a backslash of text and the closing quote, so
    `"C:\\"` is the phrase `C:\\`.
    """
    if open_ >= len(chars) or not _is_bare(chars[open_], '"'):
        return None
    if open_ < last_quote:
        close = open_ + 1
        while not _is_bare(chars[close], '"'):
            close += 1
        return chars[open_ + 1 : close], close + 1
    if open_ >= last_escaped_quote:
        return None
    escaped = open_ + 1
    while not (chars[escaped].escaped and chars[escaped].char == '"'):
        escaped += 1
    return [*chars[open_ + 1 : escaped], _Char("\\", False)], escaped + 1


def _term_of(token: _Token) -> _Term | None:
    """
    The token's term; None when it holds no word. A word still holds a field up to its first bare colon. The term is
    the start of a word when its last character is a bare `*`.
    """
    if token.quoted:
        text = token.chars
    else:
        colon = next((i for i, it in enumerate(token.chars) if _is_bare(it, ":")), -1)
        text = token.chars[colon + 1 :]
    words = [it for it in _gap().split("".join(it.char for it in text).lower()) if it]
    if not words:
        return None
    prefix = _is_bare(text[-1], "*")
    oracle_text = (
        " ".join([*(f"{{{it}}}" for it in words[:-1]), f"{words[-1]}%"]) if prefix else f"{{{' '.join(words)}}}"
    )
    return _Term(words, _BOUNDARY_BEFORE + _BETWEEN.join(words) + ("" if prefix else _BOUNDARY_AFTER), oracle_text)


def _last_index(items: Sequence[_Char], test: Callable[[_Char], bool]) -> int:
    for i in range(len(items) - 1, -1, -1):
        if test(items[i]):
            return i
    return -1
