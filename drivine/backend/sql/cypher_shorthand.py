from __future__ import annotations

import re
from collections.abc import Callable
from typing import NamedTuple

_SHORTHAND = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\.(?:`([^`]+)`|([A-Za-z_][A-Za-z0-9_]*))")
"""`alias.name` or ``alias.`a name` ``."""

_BOOLEAN_COMPARED = re.compile(
    r"([ \t\n\x0b\f\r]*(?:=|<>|!=)[ \t\n\x0b\f\r]*)(true|false)(?![A-Za-z0-9_])", re.IGNORECASE
)
"""An equality or inequality with `true` or `false` that follows a property, in any case: the operator with its
space, and the boolean."""

_COLUMNS = frozenset({"nid", "labels", "props", "stamp"})
"""What a row of a node has of its own; a predicate that names one means the column."""


class _Read(NamedTuple):
    """An `alias.name` of the root as the store reads it, and the index after it in the predicate."""

    text: str
    end: int


def _continues_name(c: str) -> bool:
    """Whether a name goes on through `c`, so a name after it is not the start of one."""
    return c.isalpha() or c.isdecimal() or c in "_."


class CypherShorthand:
    """
    Rewrites a predicate the caller wrote, with a property of the root written as Cypher writes it, `issue.state` or
    ``issue.`metadata.source` ``, to read that property. So the plain comparisons a Cypher predicate makes are the same
    text on a SQL store; anything more is SQL, over the root's columns `issue.props`, `issue.labels` and
    `issue.stamp`. Text inside a string literal is left as it is. A property compared with `true` or `false`
    (`issue.locked = true`) is compared with the text a store reads a JSON boolean as, since SQL compares no text with
    a boolean. drivine4j's `CypherShorthand`, rule for rule.
    """

    def __init__(self, alias: str, row: str, property: Callable[[str, str], str]) -> None:
        """
        `alias` is what the predicate calls the root, `row` what the statement calls the root's row, and `property` how
        the store reads the property of a row, given the row and the property's name.
        """
        self._alias = alias
        self._row = row
        self._property = property

    def rewrite(self, predicate: str) -> str:
        """`predicate` with each `alias.name` of the root read as that property, or as that column of the row."""
        out: list[str] = []
        at = 0
        while at < len(predicate):
            if predicate[at] == "'":
                stop = self._end_of_literal(predicate, at)
                out.append(predicate[at:stop])
                at = stop
                continue
            read = self._root_at(predicate, at)
            if read is not None:
                out.append(read.text)
                at = read.end
            else:
                out.append(predicate[at])
                at += 1
        return "".join(out)

    def _root_at(self, predicate: str, at: int) -> _Read | None:
        """
        The `alias.name` of the root that starts at `at`, as the store reads it: None where none does, as where a name
        goes on from before `at`, the alias is another's, or the name is a function's.
        """
        if at > 0 and _continues_name(predicate[at - 1]):
            return None
        match = _SHORTHAND.match(predicate, at)
        if match is None or match.group(1) != self._alias or predicate[match.end() : match.end() + 1] == "(":
            return None
        key = match.group(2) or match.group(3)
        # One of the row's own columns is the column; any other name is a property.
        if key in _COLUMNS:
            return _Read(f"{self._row}.{key}", match.end())
        # A property compared with a boolean is compared with the boolean's text, which is what a property is read as.
        compared = _BOOLEAN_COMPARED.match(predicate, match.end())
        if compared is None:
            return _Read(self._property(self._row, key), match.end())
        return _Read(
            f"{self._property(self._row, key)}{compared.group(1)}'{compared.group(2).lower()}'", compared.end()
        )

    @staticmethod
    def _end_of_literal(predicate: str, start: int) -> int:
        """The index after the string literal that opens at `start`; a doubled quote is part of it."""
        end = start + 1
        while end < len(predicate):
            if predicate[end] != "'":
                end += 1
            elif predicate[end + 1 : end + 2] == "'":
                end += 2
            else:
                break
        return min(end + 1, len(predicate))
