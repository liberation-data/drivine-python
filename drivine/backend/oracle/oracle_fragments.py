"""
What the shared module's fragments on Oracle ask of the statement they are written into, beside their SQL
(drivine-cypher-spec#235; drivine4j's `OracleSelect.hinted`): the optimizer hints its outermost `SELECT` or `DELETE`
carries, without which Oracle 23 can give a comparison the wrong truth (`NO_JSON_TABLE_TRANSFORM`), and the graph's
stored functions its terms call. A filter or an order of the query DSL is handed to the backend as text, which it writes
into a load, a count, a delete, a patch or a search; so what it asks travels with its text, as a comment of its own
(`noted`), which the statement it ends up in takes out again: `hinted` writes the hints where Oracle reads them, and
`called` says which functions the statement calls. Oracle reads the comment as a comment, so a text never written into
a statement still runs as it is. Drivine for TypeScript's OracleFragments.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from drivine.backend.graph_statement import StoredFunctionCalls
from drivine.backend.sql.fragments import Fragments

_NOTE = re.compile(r"/\*drivine:(hint|calls) ((?:(?!\*/).)+?)\*/", re.DOTALL)
"""A comment `noted` writes: what one fragment asks, a hint (any text but the comment's end) or a function it calls."""

_LEADING_KEYWORD = re.compile(r"\b(SELECT|DELETE)\b", re.IGNORECASE)
"""The keyword a statement's hints follow: its first `SELECT` or `DELETE`, the outermost."""


def noted(sql: str | None, fragments: Fragments) -> str | None:
    """
    `sql`, a filter or an order the fragments wrote, with what they ask of its statement before it: each hint in order,
    and each stored function the terms call. None for None, and `sql` itself where they ask nothing.
    """
    if sql is None:
        return None
    notes = [
        *(f"/*drivine:hint {it}*/" for it in fragments.hints),
        *(["/*drivine:calls nullsEqual*/"] if fragments.calls_stored_nulls_equal else []),
        *(["/*drivine:calls era*/"] if fragments.calls_stored_era else []),
        *(["/*drivine:calls temporalArithmetic*/"] if fragments.calls_stored_temporal_arithmetic else []),
    ]
    return "".join(notes) + sql


def hinted(sql: str, more: Iterable[str] = ()) -> str:
    """
    `sql`, one statement, with the hints the fragments in it ask for (`noted`) taken out and written once each, in the
    order first asked, as `/*+ A B */` after its first `SELECT` or `DELETE`, the outermost: drivine4j's
    `OracleSelect.hinted`. `more` are hints of its own fragments, asked before those. `sql` as it is where none are
    asked. What the fragments call is left in, for `called`.
    """
    hints = dict.fromkeys(more)

    def taken(note: re.Match[str]) -> str:
        if note.group(1) != "hint":
            return note.group(0)
        hints.setdefault(note.group(2))
        return ""

    text = _NOTE.sub(taken, sql)
    if not hints:
        return text
    keyword = _LEADING_KEYWORD.search(text)
    if keyword is None:
        return text
    return f"{text[: keyword.end()]} /*+ {' '.join(hints)} */{text[keyword.end() :]}"


def called(text: str, graph: str, more: Fragments | None = None) -> tuple[str, StoredFunctionCalls | None]:
    """
    `text`, a statement of the graph `graph`, with the stored functions the fragments in it call (`noted`) taken out,
    and which they are, with those `more`, its own fragments, call; None where it calls none. See OracleConnection.
    """
    names: set[str] = set()

    def taken(note: re.Match[str]) -> str:
        if note.group(1) != "calls":
            return note.group(0)
        names.add(note.group(2))
        return ""

    taken_text = _NOTE.sub(taken, text)
    calls = StoredFunctionCalls(
        graph,
        nulls_equal="nullsEqual" in names or (more is not None and more.calls_stored_nulls_equal),
        era="era" in names or (more is not None and more.calls_stored_era),
        temporal_arithmetic="temporalArithmetic" in names
        or (more is not None and more.calls_stored_temporal_arithmetic),
    )
    return taken_text, calls if calls.nulls_equal or calls.era or calls.temporal_arithmetic else None
