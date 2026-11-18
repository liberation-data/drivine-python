"""
The parts of one statement the object manager writes on a SQL store whose values the shared module compares, sorts and
binds: its fragments (the module's README, "Fragments"; liberation-data/drivine-cypher-spec#235). A port of
drivine4j's Fragments.

Each is a Cypher expression over a row of the statement's SQL, compiled by the module on the store's layout, so a
temporal value is compared by its key, a key that begins with `$` is read as it is held, an equality is answered by
the store's index, and a value is bound, as a compiled statement does. The statement's joins, its rows and how it reads
them back stay the object manager's own.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Literal

from drivine.backend.sql.value_layout import prepared
from drivine.cypher.shared_compiler import (
    FragmentPart,
    FragmentVariable,
    SharedStore,
    SqlFragment,
    compile_fragment,
)
from drivine.drivine_error import DrivineError

FRAGMENT_ROOT_ROW = "n_"
"""
What a query DSL statement on a SQL store calls its root's row once it asks for fragments: an alias that ends with `_`,
as the module takes no other (its README, "Fragments"). Drivine for TypeScript names it alike, in the DSL's own reads
alone (a load, a count, a delete, a patch's where, a group), so a statement written otherwise keeps its `n`.
"""

NODE = "n"
"""What the Cypher of a fragment the library writes itself calls the node it is over."""

RESERVED = "_cy_"
"""
The compiler's reserved prefix, which the module binds a fragment's parameters under: no caller binds a name that
begins with it. On Oracle a key compared by its index is bound as `_cyk_<name>`, outside it, so a caller's name that
is one of those is refused as bound twice instead (see `bound_beside`).
"""

_QUOTED = r"`(?:[^`]|``)*`|'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\""
"""A quoted name or a text of Cypher, a backtick doubled or a quote escaped in it: no parameter is named within one."""


class Names:
    """
    The names of the parameters of one fragment the library writes itself (an order, a value, a having): `$p` is
    `$f<n>_p`, apart from every other fragment's of the statement, as each binds its own.
    """

    def __init__(self, number: int) -> None:
        self.number = number

    def __call__(self, name: str) -> str:
        """`name` as the fragment's Cypher writes it: `$f<n>_<name>`."""
        return f"${self.given_name(name)}"

    def given_name(self, name: str) -> str:
        """`name` as the fragment binds it, without its `$`."""
        return f"f{self.number}_{name}"

    def given(self, parameters: Mapping[str, Any]) -> dict[str, Any]:
        """`parameters` under the names the fragment gives them."""
        return {self.given_name(name): value for name, value in parameters.items()}

    def named(self, cypher: str, parameters: Iterable[str]) -> str:
        """
        `cypher` with each of `parameters`, `$p`, written as the fragment names it: not where it is part of a longer
        name (`$pq`), nor within a quoted name or a text, as a property may be named `$p` and a text may hold it.
        """
        names = sorted(parameters, key=len, reverse=True)
        if not names:
            return cypher
        pattern = re.compile(rf"({_QUOTED})|\$({'|'.join(map(re.escape, names))})(?!\w)")
        return pattern.sub(lambda it: it.group(1) if it.group(1) is not None else self(it.group(2)), cypher)


Nulls = Literal["FIRST", "LAST"]


class Fragments:
    """
    The fragments of one statement, its root's row `row` of `labels` on the graph `graph` of `store`: one instance per
    statement, so the parameters of two of its fragments are named apart and bound once each. `indexed` are the
    graph's indexed properties, each a label and a property, which Oracle's labelled indexes answer an equality with.

    `parameters` are what the fragments written so far bind, under the names the module gives them (`_cy_<name>`), for
    the statement to bind beside its own (see `bound_beside`); `hints`, in the order they were first asked for and each
    once, are the optimizer hints the statement's outermost `SELECT` or `DELETE` carries (see oracle_fragments.hinted);
    `non_finite` whether a value one gives may be NaN or an infinity; and on Oracle the `calls_stored_*` flags whether
    one calls the graph's stored function of that name, which the session must be able to call (see OracleConnection).
    """

    def __init__(
        self,
        store: SharedStore,
        graph: str,
        row: str = FRAGMENT_ROOT_ROW,
        labels: Sequence[str] = (),
        indexed: Iterable[tuple[str, str]] = (),
        nulls_equal_stored: bool = True,
        era_stored: bool = True,
    ) -> None:
        self.store: SharedStore = store
        self.graph = graph
        self.row = row
        self.labels = tuple(labels)
        self.indexed = tuple(sorted(set(indexed)))
        self.nulls_equal_stored = nulls_equal_stored
        self.era_stored = era_stored
        self.parameters: dict[str, Any] = {}
        self.hints: list[str] = []
        self.non_finite = False
        self.calls_stored_nulls_equal = False
        self.calls_stored_era = False
        self.calls_stored_temporal_arithmetic = False
        self._count = 0

    def names(self) -> Names:
        """The names of the next fragment's parameters, apart from every other's of the statement."""
        self._count += 1
        return Names(self._count)

    def where(self, cypher: str | None, parameters: Mapping[str, Any], alias: str = NODE) -> str | None:
        """
        The condition the query DSL's where is, written by its QueryRenderer as the Cypher it writes for Neo4j over
        the root, `alias` in it, compiled whole as one fragment: so a condition on a related node is the module's
        `EXISTS`, and a negation Cypher's. Its parameters keep the DSL's names (`$_q<n>`), which one renderer for the
        statement numbers apart. None where there is no condition.
        """
        if cypher is None:
            return None
        return self.compiled("condition", cypher, parameters, alias, self.labels).term

    def after(self, cypher: str, parameters: Mapping[str, Any], alias: str = NODE) -> str:
        """
        The condition that keeps the rows after a keyset cursor: QueryRenderer's keyset on the Neo4j grammar, as the
        graph engines run it, compiled as one fragment, its parameters the DSL's.
        """
        return self.compiled("condition", cypher, parameters, alias, self.labels).term

    def condition(
        self, cypher: str | Callable[[Names], str], parameters: Mapping[str, Any] | None = None, row: str | None = None
    ) -> str:
        """
        A condition the library writes itself over the node `row` (the root's by default), `n` in it, with
        `parameters` named apart (see Names): `cypher` either names them itself, given the Names, or writes them as
        `$p`, which are named apart for it. The root's labels are given, so an equality is answered by their index.
        """
        names = self.names()
        given = dict(parameters or {})
        text = cypher(names) if callable(cypher) else names.named(cypher, given)
        labels = self.labels if row is None or row == self.row else ()
        return self.compiled("condition", text, names.given(given), NODE, labels, row).term

    def order(
        self,
        cypher: str,
        descending: bool = False,
        parameters: Mapping[str, Any] | None = None,
        alias: str = NODE,
        row: str | None = None,
    ) -> list[str]:
        """
        The terms an `ORDER BY` sorts by `cypher` over the node `row` (the root's by default), `alias` in it,
        descending where asked, with `parameters` named apart (see Names).
        """
        names = self.names()
        given = dict(parameters or {})
        fragment = self.compiled(
            "order", names.named(cypher, given), names.given(given), alias, (), row, descending=descending
        )
        return list(fragment.terms)

    def ordered(
        self,
        cypher: str,
        descending: bool = False,
        nulls: Nulls | None = None,
        parameters: Mapping[str, Any] | None = None,
        alias: str = NODE,
        row: str | None = None,
    ) -> list[str]:
        """
        The terms an `ORDER BY` sorts by `cypher`, descending where asked, its nulls first or last where `nulls`
        places them: a term of whether the value is null, its own fragment, comes first, descending for FIRST and
        ascending for LAST, as true sorts after false. Otherwise its nulls are where Cypher sorts them.
        """
        placed: list[str] = []
        if nulls is not None:
            placed = self.order(f"{cypher} IS NULL", nulls == "FIRST", parameters, alias, row)
        return placed + self.order(cypher, descending, parameters, alias, row)

    def value(
        self, cypher: str, parameters: Mapping[str, Any] | None = None, alias: str = NODE, row: str | None = None
    ) -> str:
        """
        The value `cypher` gives over the node `row` (the root's by default), `alias` in it, as the store's JSON holds
        it: for the statement to group or bind by, never to compare or read back. Its parameters are named apart.
        """
        names = self.names()
        given = dict(parameters or {})
        return self.compiled("value", names.named(cypher, given), names.given(given), alias, (), row).term

    def compiled(
        self,
        part: FragmentPart,
        cypher: str,
        parameters: Mapping[str, Any],
        alias: str,
        labels: Sequence[str],
        row: str | None = None,
        descending: bool = False,
    ) -> SqlFragment:
        """
        The fragment `cypher` is, over the node `row` (the root's by default) that it calls `alias`, of `labels`,
        compiled and taken: its parameters bound as a save holds them (a UUID as its text, a model as its JSON: see
        value_layout.prepared), its hints and whether it may give NaN kept.
        """
        fragment = compile_fragment(
            self.store,
            self.graph,
            part,
            cypher,
            {alias: FragmentVariable(row or self.row, tuple(labels))},
            {name: prepared(it) for name, it in parameters.items()},
            descending=descending,
            indexed_properties=self.indexed,
            nulls_equal_stored=self.nulls_equal_stored,
            era_stored=self.era_stored,
        )
        self.take(fragment)
        return fragment

    def take(self, fragment: SqlFragment) -> None:
        """
        `fragment`'s parameters bound in the statement, each once, and its hints, its NaN and the stored functions it
        calls kept for it.
        """
        # Each name checked before any is bound, so a fragment refused binds none of its own.
        for name in fragment.parameters:
            if name in self.parameters:
                raise DrivineError(f"The parameter {name} is bound already.")
        self.parameters.update(fragment.parameters)
        for hint in fragment.hints:
            if hint not in self.hints:
                self.hints.append(hint)
        self.non_finite = self.non_finite or fragment.non_finite
        self.calls_stored_nulls_equal = self.calls_stored_nulls_equal or fragment.calls_stored_nulls_equal
        self.calls_stored_era = self.calls_stored_era or fragment.calls_stored_era
        self.calls_stored_temporal_arithmetic = (
            self.calls_stored_temporal_arithmetic or fragment.calls_stored_temporal_arithmetic
        )

    def bound_beside(self, own: Mapping[str, Any]) -> dict[str, Any]:
        """
        The statement's parameters: the caller's `own`, and the fragments', each name bound once. A caller's name
        that begins with `_cy_`, which the module binds its own under, is refused, as one bound twice is.
        """
        for name in own:
            if name.startswith(RESERVED):
                raise DrivineError(
                    f"The parameter {name} begins with {RESERVED}, which Drivine binds the shared compiler's under: "
                    "give it another name."
                )
            if name in self.parameters:
                raise DrivineError(f"The parameter {name} is bound already.")
        return {**own, **self.parameters}


def property_of(key: str, alias: str = NODE) -> str:
    """
    `key` as Cypher reads a property of `alias` by it: quoted, a backtick in it doubled. An empty name is refused, as
    the graph engines' Cypher refuses it.
    """
    if not key:
        raise DrivineError("A property name is empty: an empty name is no property a statement can read.")
    return f"{alias}.`{key.replace('`', '``')}`"


def same_key(a: str, b: str) -> str:
    """
    The node keys `a` and `b` equal, compared with `+ 0` on one side and again the other way round, as the module's
    compiler joins a node back to the node table on Oracle: compared column to column, Oracle (23.26) may eliminate the
    join and read the other row by the key it dropped, or end the server process where a filter reads an indexed
    property of it (ORA-07445). drivine4j's OracleSql.sameKey.
    """
    return f"({a} = {b} + 0 AND {b} = {a} + 0)"
