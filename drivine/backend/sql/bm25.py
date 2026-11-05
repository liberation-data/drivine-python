from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from drivine.backend.sql.text_query import TextQuery

K1 = 1.2
"""How soon a term's count stops adding to its score: Lucene's."""

B = 0.75
"""How much a text's length, against the average, weighs its counts down: Lucene's."""


@dataclass(frozen=True)
class Bm25Dialect:
    """How a store writes what `bm25_ranked` asks of it."""

    count: Callable[[str, str], str]
    """
    How often a term occurs in a text, the term bound as a parameter: each time, where it is written again at once too,
    so not taking the character after one (drivine4j#607).
    """
    holds: Callable[[str, str], str]
    """Whether a term occurs in a text at all, the term bound as a parameter: cheaper than `count` where it can tell."""
    ln: Callable[[str], str]
    """The natural logarithm of a number."""
    real: Callable[[str], str]
    """A number as floating point."""


def bm25_ranked(candidates: str, label: str, query: TextQuery, sql: Bm25Dialect) -> str:
    """
    The nodes whose text meets the query, each scored by BM25, the score Lucene, and so Neo4j's full-text index, gives
    it, against the best of them, so the best scores one: a query of `nid` and `score` (drivine4j#604). Counted alone, a
    common word outweighed the word a query is about, and a long text won by holding more of everything. The statement
    is Drivine for TypeScript's `bm25Ranked`, byte for byte, as the goldens hold it.

    `candidates` is a query of `nid`, `text` and `len` for the nodes of the label that may hold a scoring term, which a
    store narrows by an index where it has one; `label` is a query of `len` for every node of the label. `text` is
    lower-cased, as a term is matched in it, and `len` is the length in characters of the text before it was: lowering
    every node's text only to measure it took most of the time a rare word's search did.

    Only the candidates that hold a scoring term are counted. Every node that holds a term is one of them, so the count
    of those that hold it, each term's rarity, is a window aggregate over them, and exact. The label's count of nodes
    and their average length are subqueries of their own, with no term matched, which no row is passed to, so a store
    works each out once: joined beside the candidates, Postgres read the label again for each candidate where it
    guessed there were few. Then the nodes that lack a required term, or hold an excluded one, are left out: they still
    count among those that hold the terms they do.
    """
    tf = [f"tf{i + 1}" for i in range(len(query.scored))]
    df = [f"df{i + 1}" for i in range(len(query.scored))]
    required = [f"r{i + 1}" for i in range(len(query.required))]
    excluded = [f"x{i + 1}" for i in range(len(query.excluded))]
    counts = [
        *zip(query.scored, tf, strict=True),
        *zip(query.required, required, strict=True),
        *zip(query.excluded, excluded, strict=True),
    ]
    # Each term bound in turn, as the statement reads them: its counts, then whether a node holds it.
    selected = ", ".join(["s.nid", "s.len", *(f"{sql.count('s.text', term)} AS {name}" for term, name in counts)])
    holding = " OR ".join(sql.holds("s.text", it) for it in query.scored)
    counted = f"SELECT {selected} FROM ({candidates}) s WHERE ({holding})"

    def figure(of: str) -> str:
        return f"(SELECT {sql.real(of)} FROM ({label}) l)"

    weighed_columns = [
        "h.*",
        f"{figure('COUNT(*)')} AS n",
        f"{figure('AVG(l.len)')} AS avglen",
        *(f"SUM(CASE WHEN h.{t} > 0 THEN 1 ELSE 0 END) OVER () AS {d}" for t, d in zip(tf, df, strict=True)),
    ]
    weighed = f"SELECT {', '.join(weighed_columns)} FROM ({counted}) h"
    score = bm25_score(
        [(f"w.{t}", f"w.{d}") for t, d in zip(tf, df, strict=True)], "w.n", "w.len", "NULLIF(w.avglen, 0)", sql.ln
    )
    conditions = [*(f"w.{it} > 0" for it in required), *(f"w.{it} = 0" for it in excluded)]
    filtered = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    scored = f"SELECT w.nid, {sql.real(score)} AS score FROM ({weighed}) w{filtered}"
    return f"SELECT b.nid, b.score / MAX(b.score) OVER () AS score FROM ({scored}) b WHERE b.score > 0"


def bm25_score(
    terms: Sequence[tuple[str, str]], nodes: str, length: str, average: str, ln: Callable[[str], str]
) -> str:
    """
    The score of a text whose `length` is against the `average` over the label's `nodes`: for each term, its count in
    the text and the count of the label's nodes that hold it, as SQL. `ln` is the store's natural logarithm. Lucene's
    weight of a term's rarity, `ln(1 + (N - df + 0.5) / (df + 0.5))`, is never below 0, so a term every node holds adds
    nothing but never takes away.
    """
    return " + ".join(
        f"{ln(f'1 + ({nodes} - {holding} + 0.5) / ({holding} + 0.5)')} * {count} * {K1 + 1} / "
        f"({count} + {K1} * ({1 - B} + {B} * {length} / {average}))"
        for count, holding in terms
    )
