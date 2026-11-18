"""
The `$name` parameters of a SQL statement, read as drivine4j's `SqlParameters.positional` reads them, and as Drivine for
TypeScript's SqlParameters does (liberation-data/drivine#789).

A parameter is a `$` that doesn't continue an identifier, followed by a name: Oracle and Postgres allow `$` inside an
identifier (`v$session`), never at its start. A name is a letter or an underscore, then letters, digits and
underscores, past ASCII too (`$größe`): a `$` ends it, and the longest name is read, so `$id` is not read in `$idList`.

Text inside a string literal (`'…'`, `n'…'`, `q'[…]'` or `nq'[…]'`, or Postgres's `e'…'`, where a backslash escapes the
quote after it), Postgres's dollar-quoted text (`$$…$$`, `$tag$…$tag$`), a quoted identifier or a comment is left as it
is. So the `'$t'` of a temporal value's tag, the `'$$score'` key of a property whose name begins with `$`, and a JSON
path such as `'$."at"."$t"'` or `'$.name'` are never read as parameters, whatever the statement binds.
"""

from __future__ import annotations

import re
from collections.abc import Callable

_NAME = re.compile(r"[^\W\d]\w*")
"""A parameter's name, read from where it begins."""

_CLOSING = {"[": "]", "(": ")", "{": "}", "<": ">"}
"""The character that closes an Oracle `q'…'` literal opened by each bracket; any other closes itself."""


def replace_parameters(sql: str, replace: Callable[[str], str | None]) -> str:
    """
    `sql` with each parameter outside quoted text given to `replace`, by its name: what it gives stands in the
    parameter's place, or the parameter is kept as it is where it gives None.
    """
    out: list[str] = []

    def replaced(name: str, text: str) -> None:
        given = replace(name)
        out.append(text if given is None else given)

    _walk(sql, out.append, replaced)
    return "".join(out)


def parameter_names(sql: str) -> set[str]:
    """The names of the parameters outside quoted text in `sql`."""
    names: set[str] = set()
    _walk(sql, lambda _: None, lambda name, _: names.add(name))
    return names


def _walk(sql: str, text: Callable[[str], None], parameter: Callable[[str, str], None]) -> None:
    """Reads `sql` once, left to right, giving `text` what is no parameter and `parameter` each parameter."""
    i = 0
    plain = 0
    while i < len(sql):
        end = _end_of_skipped(sql, i)
        if end >= 0:
            i = end
            continue
        name = _name_at(sql, i + 1) if sql[i] == "$" and not _continues_identifier(sql, i) else None
        if name is None:
            i += 1
            continue
        text(sql[plain:i])
        parameter(name, sql[i : i + 1 + len(name)])
        i += 1 + len(name)
        plain = i
    text(sql[plain:])


def _end_of_skipped(sql: str, start: int) -> int:
    """The index after the quoted text or comment that opens at `start`; -1 where none does."""
    c = sql[start]
    if c in ("'", '"'):
        return _end_of_quoted(sql, start, c)
    prefix = _literal_prefix(sql, start)
    if prefix is not None:
        kind = prefix.lower()
        if kind.endswith("q"):
            return _end_of_alternative_quoted(sql, start + len(prefix) - 1)
        return _end_of_quoted(sql, start + len(prefix), "'", kind == "e")
    tag = _dollar_tag(sql, start)
    if tag is not None:
        return sql.index(tag, start + len(tag)) + len(tag)
    if sql.startswith("--", start):
        end = sql.find("\n", start)
        return len(sql) if end < 0 else end
    if sql.startswith("/*", start):
        end = sql.find("*/", start + 2)
        return len(sql) if end < 0 else end + 2
    return -1


def _name_at(sql: str, start: int) -> str | None:
    """The name of a parameter that begins at `start`, past its `$`; None where none does."""
    found = _NAME.match(sql, start)
    return found.group(0) if found else None


def _continues_identifier(sql: str, index: int) -> bool:
    """Whether the character before `index` is part of an identifier, which the one at `index` would then continue."""
    if index == 0:
        return False
    before = sql[index - 1]
    return before.isalnum() or before in "_$#"


def _literal_prefix(sql: str, start: int) -> str | None:
    """
    The prefix of a national, alternatively quoted or escaped literal that opens at `start` (`n`, `q`, `nq` or `e`, in
    either case), where one does.
    """
    if _continues_identifier(sql, start):
        return None
    ahead = sql[start : start + 3].lower()
    for it in ("nq'", "q'", "n'", "e'"):
        if ahead.startswith(it):
            return it[:-1]
    return None


def _dollar_tag(sql: str, start: int) -> str | None:
    """
    The tag of a Postgres dollar quote that opens at `start`, `$$` or `$tag$`, the dollars and all, where one does. A
    quote is one only where the tag comes again to close it: so `$a$b` is still the parameter `a` and the text `$b`,
    as Oracle reads it. `$$` before a name is a PL/SQL inquiry directive (`$$PLSQL_UNIT`), not a quote. That is
    Oracle's rule, and it is kept on Postgres too, as drivine4j keeps it: there a body written `$$BEGIN … $$` is not
    skipped, so its `$name`s are read as parameters. No statement Drivine writes opens a dollar quote so.
    """
    if sql[start] != "$" or _continues_identifier(sql, start):
        return None
    name = _name_at(sql, start + 1) or ""
    close = start + 1 + len(name)
    if close >= len(sql) or sql[close] != "$":
        return None
    if name == "" and _name_at(sql, close + 1) is not None:
        return None
    tag = sql[start : close + 1]
    return tag if sql.find(tag, close + 1) >= 0 else None


def _end_of_quoted(sql: str, start: int, quote: str, escapes: bool = False) -> int:
    """
    The index after the literal or quoted identifier that opens at `start`; a doubled quote is part of it, and so is
    one after a backslash where `escapes` (Postgres's `e'it\\'s'`).
    """
    i = start + 1
    while i < len(sql):
        if escapes and sql[i] == "\\":
            i += 2
        elif sql[i] != quote:
            i += 1
        elif sql[i + 1 : i + 2] == quote:
            i += 2
        else:
            return i + 1
    return len(sql)


def _end_of_alternative_quoted(sql: str, start: int) -> int:
    """The index after an Oracle `q'[…]'` literal whose `q` is at `start`."""
    if start + 2 >= len(sql):
        return len(sql)
    opening = sql[start + 2]
    end = sql.find(f"{_CLOSING.get(opening, opening)}'", start + 3)
    return len(sql) if end < 0 else end + 2
