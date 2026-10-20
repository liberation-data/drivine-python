from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from drivine.drivine_error import DrivineError

_GAP = re.compile(r"\W+")
"""What stands between two words: a run of anything but letters, digits and underscores."""
_GAP_CHAR = re.compile(r"^\W$")

_SPACES = " \t\n\u000b\u000c\r"
"""The characters that end a term."""

_BOUNDARY_BEFORE = "(^|[^[:alnum:]_])"
_BOUNDARY_AFTER = "($|[^[:alnum:]_])"

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

    pattern: str
    oracle_text: str


@dataclass
class _Parts:
    """The parts of a query as they are read, each kind as expressions and as Oracle Text."""

    scored: list[str] = field(default_factory=lambda: [])
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
        return TextQuery(parts.scored, parts.required, parts.excluded, text)


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
        if query[at] == "\\" and following is not None and _GAP_CHAR.match(following):
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
    words = [it for it in _GAP.split("".join(it.char for it in text).lower()) if it]
    if not words:
        return None
    prefix = _is_bare(text[-1], "*")
    oracle_text = (
        " ".join([*(f"{{{it}}}" for it in words[:-1]), f"{words[-1]}%"]) if prefix else f"{{{' '.join(words)}}}"
    )
    return _Term(_BOUNDARY_BEFORE + "[^[:alnum:]_]+".join(words) + ("" if prefix else _BOUNDARY_AFTER), oracle_text)


def _last_index(items: Sequence[_Char], test: Callable[[_Char], bool]) -> int:
    for i in range(len(items) - 1, -1, -1):
        if test(items[i]):
            return i
    return -1
