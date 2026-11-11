"""
The Cypher spec's host.yaml (tests/cypher): what drivine-python's own code does with the values compiled SQL hands it,
with no store, as drivine4j's `HostSpecTest` and Drivine for TypeScript's `HostSpec` run it. Each `function` step calls
one of the functions Drivine registers on SQLite, on an in-memory database; each `read` step reads a column's text as a
connection reads a compiled statement's rows. Each case's `pairs` name store cases of the other spec files whose
statements rely on its steps: one of them compiles on SQLite to SQL that calls each function its steps call, and one
compiles on each store its steps read with the `nonFinite` each read step says. The three libraries read the same file.

A case or step that can't pass yet is held: see HELD.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import apsw
import psycopg
import pytest
from psycopg.adapt import AdaptersMap
from psycopg.pq import Format

from drivine.backend.sqlite import sqlite_functions
from drivine.connection.postgres import postgres_connection
from drivine.connection.postgres.postgres_connection import PostgresConnection
from drivine.connection.sqlite import sqlite_connection
from drivine.connection.sqlite.sqlite_connection import SqliteConnection
from drivine.connection.sqlite.sqlite_session import SqliteSession
from drivine.cypher.shared_compiler import SharedStore, compile_cypher
from drivine.cypher.sql.compiled_cypher import CompiledCypher
from drivine.query.query_specification import QuerySpecification
from drivine.query.statement import Statement
from tests.cypher.cypher_spec import Step, canonical, expanded, load, read

CASES: list[dict[str, Any]] = read("host.yaml")["cases"]

SHAPES = [{"function", "args", "result"}, {"function", "ascending"}, {"read", "value"}]
"""The keys of each kind of step, beside its optional `note`: a function's result, its order, and a read."""

READ = {"store", "nonFinite", "column"}

FUNCTION = re.compile(r"^(?:drivine_[a-z_]+|regexp)$")
"""Drivine's functions, and `regexp`, which SQLite calls, as `regexp(pattern, text)`, for its operator `text REGEXP
pattern`."""

REGEXP_OPERATOR = re.compile(r"\bREGEXP\b", re.IGNORECASE)
QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
"""SQL's quoted texts and names: a quote inside one is written twice, so splits it into two that touch, each matched
here."""

COMPARED = Step(cypher="")
"""How a value is compared: of its kind (true is not 1, and 2 is not 2.0), NaN the same as NaN."""


@dataclass(frozen=True)
class Hold:
    """
    What of a case fails here until an issue lands, and is checked to fail until it does: a hold whose steps or pairs
    pass fails, so it is taken off as soon as it can be.
    """

    issue: int
    """The issue of drivine-python's whose fix makes them pass."""
    steps: tuple[int, ...] = ()
    """The steps, from 1, that give another result than the spec's."""
    pairs: bool = False
    """The pairs check, where the compiler writes SQL that doesn't call the function, or doesn't say the `nonFinite`
    the read step reads."""


HELD: dict[str, list[Hold]] = {}
"""The holds of each case, by its name: see Hold. As Drivine for TypeScript's."""


class Mismatch(AssertionError):
    """A step or a pairs check that gives, or fails, otherwise than the spec says."""


def without_note(step: dict[str, Any]) -> dict[str, Any]:
    return {key: it for key, it in step.items() if key != "note"}


def described(step: dict[str, Any]) -> str:
    return repr(without_note(step))


def held_step(name: str, index: int) -> int | None:
    return next((it.issue for it in HELD.get(name, []) if index + 1 in it.steps), None)


def held_pairs(name: str) -> int | None:
    return next((it.issue for it in HELD.get(name, []) if it.pairs), None)


async def held(issue: int | None, where: str, check: Callable[[], Any]) -> None:
    """
    Runs `check`, which must fail where `issue` holds it: one held that passes fails, so its hold is taken off. Held
    only where it fails as the spec says it does not (an AssertionError, Mismatch among them): anything else the runner
    raises is raised on, so that a hold does not hide the runner's own crash.
    """
    if issue is None:
        await _run(check)
        return
    try:
        await _run(check)
    except AssertionError:
        return
    pytest.fail(f"{where} is held for liberation-data/drivine-python#{issue}, and passes: take the hold off (HELD).")


async def _run(check: Callable[[], Any]) -> None:
    result = check()
    if hasattr(result, "__await__"):
        await result


STEPS = [(case, index) for case in CASES for index in range(len(case["steps"]))]


@pytest.mark.parametrize(("case", "index"), STEPS, ids=[f"{case['name']}, step {index + 1}" for case, index in STEPS])
async def test_step(monkeypatch: pytest.MonkeyPatch, case: dict[str, Any], index: int) -> None:
    step = case["steps"][index]
    where = f"{case['name']}, step {index + 1}: {described(step)}"
    await held(held_step(case["name"], index), where, lambda: check_step(monkeypatch, step, where))


@pytest.mark.parametrize("case", CASES, ids=[it["name"] for it in CASES])
async def test_pairs(case: dict[str, Any]) -> None:
    await held(held_pairs(case["name"]), f"{case['name']}: pairs", lambda: paired(case))


def test_holds_nothing_for_a_case_the_file_has_no_longer() -> None:
    assert [it for it in HELD if not any(case["name"] == it for case in CASES)] == []


async def check_step(monkeypatch: pytest.MonkeyPatch, step: dict[str, Any], where: str) -> None:
    """The step gives what it says, and is of a shape the spec's README gives."""
    keys = set(without_note(step))
    if keys not in SHAPES:
        raise ValueError(f"{where}: a step has the keys of one of {SHAPES}, and an optional note")
    if "read" in step:
        if set(step["read"]) != READ:
            raise ValueError(f"{where}: a read has the keys {READ}")
        await check_read(monkeypatch, step["read"], step["value"], where)
        return
    with database() as connection:
        check_function(connection, step, where)


@contextmanager
def database() -> Iterator[apsw.Connection]:
    """An in-memory SQLite, with each function registered on it as Drivine registers them on a connection."""
    connection = apsw.Connection(":memory:")
    try:
        sqlite_functions.register(connection)
        yield connection
    finally:
        connection.close()


def check_function(connection: apsw.Connection, step: dict[str, Any], where: str) -> None:
    """The step's function, called with its `args`, gives its `result`; or, called on each of its `ascending`,
    ascends."""
    name: str = step["function"]
    if not FUNCTION.match(name):
        raise ValueError(f"{where}: no function of Drivine's")
    if "ascending" in step:
        values = expanded(step["ascending"])
        if len(values) < 2:
            raise ValueError(f"{where}: ascending has two values or more, each before the next")
        for before, after in zip(values, values[1:], strict=False):
            found = call(connection, f"SELECT {name}(?) < {name}(?)", [before, after])
            assert found == 1, f"{where}: {before} is not before {after}"
        return
    args = expanded(step["args"])
    found = call(connection, f"SELECT {name}({', '.join('?' for _ in args)})", args)
    expected = expanded(step["result"])
    assert canonical(found, COMPARED) == canonical(expected, COMPARED), (
        f"{where}\nexpected: {expected!r}\nactual: {found!r}"
    )


def call(connection: apsw.Connection, sql: str, args: list[Any]) -> Any:
    """The one value `sql` gives, run with `args`."""
    try:
        return connection.execute(sql, args).fetchall()[0][0]
    except Exception as error:
        # The function fails, where the spec gives what it gives.
        raise Mismatch(f"{sql}: {error}") from error


async def check_read(monkeypatch: pytest.MonkeyPatch, read: dict[str, Any], value: Any, where: str) -> None:
    """The read's column, of a compiled statement's rows on its store, reads as `value`, as a connection gives it."""
    text = column(read["column"])
    rows = await rows_of(monkeypatch, read["store"], text, bool(read["nonFinite"]), where)
    expected = [expanded(value)]
    assert canonical(rows, COMPARED) == canonical(expected, COMPARED), (
        f"{where}\nexpected: {expected!r}\nactual: {rows!r}"
    )


def column(value: Any) -> str:
    """A column's text: a text, or a list of texts and `$repeat`s written one after the other."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(str(expanded(it)) for it in value)  # pyright: ignore[reportUnknownVariableType]
    raise ValueError(f"No column text of {value!r}")


def compiled_as(monkeypatch: pytest.MonkeyPatch, module: Any, sql: str, parameters: Any, non_finite: bool) -> None:
    """Has the connection of `module` compile each Cypher statement to `sql`, saying `non_finite` of it."""

    def compiled(spec: QuerySpecification[Any], *_: Any) -> tuple[QuerySpecification[Any], CompiledCypher | None]:
        if spec.statement is None or spec.statement.language != "CYPHER":
            return spec, None
        return QuerySpecification[Any](Statement(sql, "SQL")).bind(parameters), CompiledCypher(
            sql, parameters, non_finite=non_finite
        )

    monkeypatch.setattr(module, "cypher_compiled", compiled)


CYPHER = QuerySpecification[Any](Statement("RETURN 1 AS x", "CYPHER"))
"""The statement each read runs: compiled to the SQL that gives the read's column (see compiled_as)."""


async def rows_of(monkeypatch: pytest.MonkeyPatch, store: str, text: str, non_finite: bool, where: str) -> list[Any]:
    """
    The rows a connection to `store` gives for a compiled statement whose one column holds `text`, as it reads a
    compiled Cypher statement's: on SQLite, on an in-memory database; on Postgres, a client whose cursor hands the text
    to the `jsonb` loader the connection set on it, as psycopg does.
    """
    with monkeypatch.context() as patched:
        if store == "sqlite":
            compiled_as(patched, sqlite_connection, "SELECT $t AS result", {"t": text}, non_finite)
            session = SqliteSession(":memory:")

            async def release(_: SqliteSession) -> None:
                session.close()

            return await SqliteConnection(session, release, "host").query(CYPHER)
        if store == "postgres":
            compiled_as(patched, postgres_connection, "SELECT %(t)s AS result", {"t": text}, non_finite)
            return await PostgresConnection(_PostgresClient(text), None, "host").query(CYPHER)
    raise ValueError(f"{where}: no reader for {store}")


JSONB = 3802


class _PostgresCursor:
    """A psycopg cursor whose one row's one column is `text`, a `jsonb` value, read by its loader."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.adapters = AdaptersMap(psycopg.adapters)
        self.description = [("result",)]

    async def __aenter__(self) -> _PostgresCursor:
        return self

    async def __aexit__(self, *_: Any) -> None:
        pass

    async def execute(self, statement: str, parameters: Any = None) -> None:
        pass

    async def fetchall(self) -> list[dict[str, Any]]:
        loader = self.adapters.get_loader(JSONB, Format.TEXT)
        assert loader is not None
        return [{"result": loader(JSONB).load(self.text.encode())}]


class _PostgresClient:
    def __init__(self, text: str) -> None:
        self.text = text

    def cursor(self, **_: Any) -> _PostgresCursor:
        return _PostgresCursor(self.text)


STORES: dict[str, SharedStore] = {"sqlite": "SQLITE", "postgres": "POSTGRES", "oracle": "ORACLE"}


def compiled_on(store: str, steps: list[Step]) -> list[CompiledCypher]:
    """The steps that run on `store`, compiled to its SQL by the shared compiler: those it refuses left out."""
    statements: list[CompiledCypher] = []
    for step in steps:
        if not step.runs_on(store):
            continue
        try:
            statements.append(compile_cypher(step.cypher, expanded(step.parameters), STORES[store], "g"))
        except Exception:  # noqa: S112
            # A step the store refuses relies on nothing here.
            continue
    return statements


def paired(case: dict[str, Any]) -> None:
    """
    The case's pairs are cases of their files, and rely on its steps: one of them compiles on SQLite to SQL that calls
    each function its steps call, and one compiles on each store its steps read with the `nonFinite` each read step
    says.
    """
    pairs: dict[str, list[str]] = case.get("pairs") or {}
    if not pairs:
        raise ValueError(f"{case['name']}: pairs no store case")
    steps: list[Step] = []
    for file, names in pairs.items():
        spec = load(file)
        for name in names:
            found = next((it for it in spec.cases if it.name == name), None)
            if found is None:
                raise ValueError(f'{case["name"]}: {file} has no case "{name}"')
            steps.extend(found.steps)
    sqlite: list[CompiledCypher] | None = None
    functions = list(dict.fromkeys(it["function"] for it in case["steps"] if "function" in it))
    for name in functions:
        sqlite = sqlite if sqlite is not None else compiled_on("sqlite", steps)

        def calls(sql: str, name: str = name) -> bool:
            return holds_regexp(sql) if name == "regexp" else f"{name}(" in sql

        if not any(calls(it.sql) for it in sqlite):
            raise Mismatch(f"{case['name']}: no step of its pairs compiles on SQLite to SQL that calls {name}")
    reads = [it["read"] for it in case["steps"] if "read" in it]
    for store, non_finite in dict.fromkeys((it["store"], bool(it["nonFinite"])) for it in reads):
        if store not in STORES:
            raise ValueError(f"{case['name']}: no compiler for {store}")
        statements = sqlite if store == "sqlite" and sqlite is not None else compiled_on(store, steps)
        if not any(it.non_finite is non_finite for it in statements):
            raise Mismatch(f"{case['name']}: no step of its pairs compiles on {store} with nonFinite {non_finite}")


def holds_regexp(sql: str) -> bool:
    """Whether `sql` holds SQLite's `REGEXP` operator, outside its quoted texts and names."""
    return REGEXP_OPERATOR.search(QUOTED.sub(" ", sql)) is not None


# The runner itself, as Drivine for TypeScript checks its own.

ADD = {"function": "drivine_add", "args": ["1", "2"], "result": "3"}
READ_STEP = {"read": {"store": "postgres", "nonFinite": False, "column": "1.5"}, "value": 1.5}


async def test_the_runner_fails_a_step_or_a_read_with_a_key_the_spec_does_not_have(monkeypatch: pytest.MonkeyPatch):
    await check_step(monkeypatch, ADD, "add")
    with pytest.raises(ValueError, match="a step has the keys"):
        await check_step(monkeypatch, {**ADD, "reslt": "3"}, "add")
    with pytest.raises(ValueError, match="a step has the keys"):
        await check_step(monkeypatch, {"function": "drivine_add", "args": ["1", "2"]}, "add")
    await check_step(monkeypatch, READ_STEP, "read")
    with pytest.raises(ValueError, match="a step has the keys"):
        await check_step(monkeypatch, {"read": READ_STEP["read"]}, "read")
    with pytest.raises(ValueError, match="a read has the keys"):
        await check_step(
            monkeypatch, {"read": {"store": "postgres", "nonfinite": False, "column": "1.5"}, "value": 1.5}, "read"
        )


async def test_the_runner_fails_a_function_drivine_does_not_register_or_a_result_it_does_not_give(
    monkeypatch: pytest.MonkeyPatch,
):
    with pytest.raises(Mismatch):
        await check_step(monkeypatch, {"function": "drivine_nothing", "args": ["1"], "result": "1"}, "nothing")
    with pytest.raises(AssertionError):
        await check_step(monkeypatch, {**ADD, "result": "4"}, "add")
    with pytest.raises(AssertionError):
        await check_step(monkeypatch, {**ADD, "result": 3}, "add")
    with pytest.raises(ValueError, match="two values or more"):
        await check_step(monkeypatch, {"function": "drivine_nothing", "ascending": ["[1]"]}, "one")
    with pytest.raises(AssertionError):
        await check_step(monkeypatch, {"function": "drivine_sort_key", "ascending": ["[2]", "[1]"]}, "two")
    with pytest.raises(AssertionError):
        await check_step(monkeypatch, {**READ_STEP, "value": 2.5}, "read")


async def test_the_runner_reads_a_whole_number_apart_from_a_float(monkeypatch: pytest.MonkeyPatch):
    read = {"store": "sqlite", "nonFinite": False, "column": "2"}
    await check_step(monkeypatch, {"read": read, "value": 2}, "read")
    with pytest.raises(AssertionError):
        await check_step(monkeypatch, {"read": read, "value": 2.0}, "read")


def test_the_runner_fails_a_case_with_no_pairs_a_pair_no_file_has_or_pairs_that_call_none_of_its_functions():
    def named(pairs: dict[str, list[str]] | None) -> dict[str, Any]:
        return {"name": "a case", "pairs": pairs, "steps": [ADD]}

    with pytest.raises(ValueError, match="pairs no store case"):
        paired(named(None))
    with pytest.raises(ValueError, match='writes.yaml has no case "No such case"'):
        paired(named({"writes.yaml": ["No such case"]}))
    with pytest.raises(Mismatch, match="compiles on SQLite to SQL that calls drivine_add"):
        paired(named({"queries.yaml": ["Returning nodes and properties"]}))
    given = {"read": {**READ_STEP["read"], "nonFinite": True}, "value": 1.5}
    reading = {"name": "a case", "pairs": {"queries.yaml": ["Returning nodes and properties"]}, "steps": [given]}
    with pytest.raises(Mismatch, match="compiles on postgres with nonFinite True"):
        paired(reading)


async def test_the_runner_calls_regexp_with_the_pattern_first_and_pairs_it_by_sql_that_holds_the_operator(
    monkeypatch: pytest.MonkeyPatch,
):
    regexp = {"queries.yaml": ["Regular expressions and more functions"]}
    steps = [
        {"function": "regexp", "args": ["b", "abc"], "result": 1},
        {"function": "regexp", "args": ["abc", "b"], "result": 0},
        {"function": "regexp", "args": [None, "a"], "result": None},
    ]
    for step in steps:
        await check_step(monkeypatch, step, "regexp")
    paired({"name": "regexp", "pairs": regexp, "steps": steps})
    with pytest.raises(AssertionError):
        await check_step(monkeypatch, {**steps[1], "result": 1}, "regexp")
    with pytest.raises(Mismatch, match="compiles on SQLite to SQL that calls regexp"):
        paired({"name": "regexp", "pairs": {"queries.yaml": ["Returning nodes and properties"]}, "steps": steps})
    with pytest.raises(ValueError, match="no function of Drivine's"):
        await check_step(monkeypatch, {**steps[0], "function": "regexpx"}, "regexp")


def test_the_runner_pairs_regexp_by_the_operator_not_by_a_text_or_a_name_that_says_regexp():
    assert holds_regexp("SELECT ('x' ->> '$') REGEXP '^a\\z'")
    assert holds_regexp("SELECT 'x' not regexp 'a'")
    assert not holds_regexp("SELECT json_quote('a REGEXP b')")
    assert not holds_regexp("SELECT json_object('regexp', 1) AS \"regexp\"")
    assert not holds_regexp("SELECT drivine_regexp_count('a', 'b')")
    assert not holds_regexp("SELECT 'it''s REGEXP', \"a\"\"REGEXP\"")
    # A backslash escapes nothing in SQL's quoted texts.
    assert holds_regexp("SELECT 'a\\' REGEXP 'b'")
    assert holds_regexp(f"SELECT '{'x' * 200_000}' REGEXP 'a'")
    doubled = "''" * 100_000
    assert not holds_regexp(f"SELECT '{doubled} REGEXP'")


async def test_the_runner_fails_a_hold_whose_steps_pass_and_does_not_hide_its_own_failing_otherwise(
    monkeypatch: pytest.MonkeyPatch,
):
    with pytest.raises(pytest.fail.Exception, match="held for liberation-data/drivine-python#1, and passes"):
        await held(1, "a held step", lambda: None)
    await held(1, "a held step", lambda: check_step(monkeypatch, {**ADD, "result": "4"}, "add"))
    await held(
        1,
        "a held step",
        lambda: check_step(monkeypatch, {"function": "drivine_nothing", "args": ["1"], "result": "1"}, "x"),
    )
    with pytest.raises(ValueError, match="a step has the keys"):
        await held(1, "a held step", lambda: check_step(monkeypatch, {**ADD, "reslt": "3"}, "add"))

    def crash() -> None:
        raise TypeError("a crash")

    with pytest.raises(TypeError, match="a crash"):
        await held(1, "a held step", crash)
