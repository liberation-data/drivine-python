"""
The shared module's fragments (its README, "Fragments"; liberation-data/drivine-cypher-spec#235) as Drivine asks for
them: each case of the spec's fragments.yaml gives the terms it pins on each SQL store, and each form of its table the
terms whose digest it pins, as the module's FragmentsTest holds each target to them, through `compile_fragment` and the
statement's Fragments (drivine/backend/sql/fragments.py). No database: the integration runner,
tests/integration/sql/test_fragments_spec.py, holds each store to Neo4j's rows.
"""

import hashlib
from datetime import date
from typing import Any

import pytest

from drivine import DrivineError
from drivine.backend.sql.fragments import FRAGMENT_ROOT_ROW, Fragments, Names, property_of, same_key
from drivine.cypher import shared_compiler
from drivine.cypher.shared_compiler import FragmentPart, FragmentVariable, SharedStore, SqlFragment, compile_fragment
from tests.cypher.fragment_spec import CONDITIONS, ORDERS, SPEC, parameters, spec_indexed, spec_variables

STORES: list[SharedStore] = ["POSTGRES", "ORACLE", "SQLITE"]
VARIABLES = spec_variables()
INDEXED = spec_indexed()
PEOPLE = {"n": FragmentVariable("people_", ("Person",))}


def part_of(case: dict[str, Any]) -> FragmentPart:
    return next(it for it in ("condition", "value", "order") if it in case)


def terms(
    store: SharedStore, part: FragmentPart, cypher: str, given: dict[str, Any] | None, descending: bool = False
) -> list[str]:
    fragment = compile_fragment(
        store, SPEC["graph"], part, cypher, VARIABLES, parameters(given), descending, indexed_properties=INDEXED
    )
    return list(fragment.terms)


def digest(found: list[str]) -> str:
    """The first 16 hexadecimal digits of the SHA-256 of the terms joined by a line feed, as the spec's table pins."""
    return hashlib.sha256("\n".join(found).encode()).hexdigest()[:16]


@pytest.mark.parametrize("store", STORES)
@pytest.mark.parametrize("case", SPEC["cases"], ids=lambda it: it["name"])
def test_each_case_gives_the_terms_it_pins_on_each_store(case: dict[str, Any], store: SharedStore):
    part = part_of(case)
    found = terms(store, part, case[part], case.get("parameters"), bool(case.get("descending", False)))
    assert found == case["sql"][store.lower()]


@pytest.mark.parametrize("store", STORES)
@pytest.mark.parametrize(("entry", "at", "condition"), CONDITIONS, ids=[it[2] for it in CONDITIONS])
def test_each_condition_of_the_table_in_each_position_gives_the_terms_its_digest_pins(
    entry: dict[str, Any], at: int, condition: str, store: SharedStore
):
    found = terms(store, "condition", condition, entry.get("parameters"))
    assert digest(found) == entry["digests"][store.lower()][at], found


@pytest.mark.parametrize("store", STORES)
@pytest.mark.parametrize(
    ("entry", "at", "descending"),
    ORDERS,
    ids=[f"{it[0]['value']}{' DESC' if it[2] else ''}" for it in ORDERS],
)
def test_each_order_of_the_table_both_ways_gives_the_terms_its_digest_pins(
    entry: dict[str, Any], at: int, descending: bool, store: SharedStore
):
    found = terms(store, "order", entry["value"], None, descending)
    assert digest(found) == entry["digests"][store.lower()][at], found


def test_the_spec_has_the_cases_and_table_the_runner_counts():
    with_rows = [it for it in SPEC["cases"] if "rows" in it]
    assert len(with_rows) + len(CONDITIONS) + len(ORDERS) == 271


@pytest.mark.parametrize("store", STORES)
def test_a_form_the_module_refuses_reaches_the_caller_as_its_refusal(store: SharedStore):
    with pytest.raises(DrivineError, match=r"The fragment API doesn't take count\(\)"):
        compile_fragment(store, "g", "condition", "count(n) > 1", PEOPLE)
    with pytest.raises(DrivineError, match="ends with _"):
        compile_fragment(store, "g", "condition", "n.k = 1", {"n": FragmentVariable("n1")})
    with pytest.raises(DrivineError, match="missing"):
        Fragments(store, "g", "people_").where("n.k = $missing", {})


def test_a_request_the_module_cant_read_is_an_error_of_its_own(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(shared_compiler, "_compiler", lambda _: lambda _: '{"error": "no request"}')
    with pytest.raises(DrivineError, match="can't read the fragment request: no request"):
        compile_fragment("SQLITE", "g", "condition", "n.k = 2", PEOPLE)


def test_a_fragment_binds_its_parameters_under_the_modules_names_as_a_statement_does():
    fragment = compile_fragment("SQLITE", "g", "condition", "n.born < $d", PEOPLE, {"d": date(1900, 1, 1)})
    assert set(fragment.parameters) == {"_cy_d"}
    assert '"s":"10000019000101"' in fragment.parameters["_cy_d"]
    assert fragment.hints == ()
    oracle = compile_fragment(
        "ORACLE", "g", "condition", "n.name = $p", PEOPLE, {"p": "Ada"}, indexed_properties=INDEXED
    )
    assert "NO_JSON_TABLE_TRANSFORM" in oracle.hints
    assert "JSON_EXISTS(people_.labels" in oracle.term
    # Without the index given, Oracle doesn't test the node's labels for it.
    plain = compile_fragment("ORACLE", "g", "condition", "n.name = $p", PEOPLE, {"p": "Ada"})
    assert "people_.labels" not in plain.term


def test_indexed_properties_are_written_on_oracle_alone():
    assert "indexedProperties" not in shared_compiler._graph("POSTGRES", "g", True, True, INDEXED)  # pyright: ignore[reportPrivateUsage]
    assert "indexedProperties" not in shared_compiler._graph("ORACLE", "g", True, True)  # pyright: ignore[reportPrivateUsage]
    assert shared_compiler._graph("ORACLE", "g", True, True, INDEXED)["indexedProperties"] == [  # pyright: ignore[reportPrivateUsage]
        ["Person", "name"],
        ["Person", "id"],
    ]


def test_the_same_request_is_answered_from_the_cache_and_the_cache_is_bounded(monkeypatch: pytest.MonkeyPatch):
    asked: list[str] = []
    entry = shared_compiler._compiler(lambda: "")  # pyright: ignore[reportPrivateUsage]

    def counted(request: str) -> str:
        asked.append(request)
        return entry(request)

    monkeypatch.setattr(shared_compiler, "_compiler", lambda _: counted)
    monkeypatch.setattr(shared_compiler, "FRAGMENTS_CACHED", 2)
    monkeypatch.setattr(shared_compiler, "_fragments_answered", type(shared_compiler._fragments_answered)())  # pyright: ignore[reportPrivateUsage]
    for _ in range(3):
        compile_fragment("SQLITE", "g", "condition", "n.k = 1", PEOPLE)
    assert len(asked) == 1
    compile_fragment("SQLITE", "g", "condition", "n.k = 2", PEOPLE)
    compile_fragment("SQLITE", "g", "condition", "n.k = 3", PEOPLE)
    assert len(shared_compiler._fragments_answered) == 2  # pyright: ignore[reportPrivateUsage]
    compile_fragment("SQLITE", "g", "condition", "n.k = 1", PEOPLE)
    assert len(asked) == 4
    # A refusal is the module's answer too, and given again as it was.
    for _ in range(2):
        with pytest.raises(DrivineError, match=r"count\(\)"):
            compile_fragment("SQLITE", "g", "condition", "count(n) > 1", PEOPLE)
    assert len(asked) == 5


def test_the_parameters_of_two_fragments_of_a_statement_are_named_apart():
    fragments = Fragments("SQLITE", "g", "people_", ["Person"])
    first = fragments.value("n.age + $p", {"p": 1})
    second = fragments.order("n.age * $p", False, {"p": 2})
    assert "$_cy_f1_p" in first
    assert "$_cy_f2_p" in second[0]
    assert fragments.parameters == {"_cy_f1_p": "1", "_cy_f2_p": "2"}
    # A name that starts another isn't taken for it.
    names = Names(3)
    assert names.named("n.a = $p AND n.b = $pq", ["p", "pq"]) == "n.a = $f3_p AND n.b = $f3_pq"
    # A condition may name its parameters itself.
    fragments.condition(lambda name: f"n.name = {name('value')}", {"value": "Ada"})
    assert "_cy_f3_value" in fragments.parameters


def test_an_order_places_its_nulls_by_a_fragment_of_its_own_first():
    fragments = Fragments("SQLITE", "g", "people_")
    first = fragments.ordered(property_of("age"), True, "FIRST")
    alone = Fragments("SQLITE", "g", "people_")
    assert first == [
        *alone.order("n.`age` IS NULL", True),
        *alone.order("n.`age`", True),
    ]
    last = Fragments("SQLITE", "g", "people_").ordered(property_of("age"), False, "LAST")
    alone = Fragments("SQLITE", "g", "people_")
    assert last == [*alone.order("n.`age` IS NULL", False), *alone.order("n.`age`", False)]
    assert Fragments("SQLITE", "g", "people_").ordered("n.age") == Fragments("SQLITE", "g", "people_").order("n.age")


def test_a_name_bound_twice_or_a_callers_name_under_the_compilers_prefix_is_refused():
    fragments = Fragments("SQLITE", "g", "people_", ["Person"])
    fragments.where("n.name = $_q0", {"_q0": "Ada"})
    with pytest.raises(DrivineError, match="The parameter _cy__q0 is bound already."):
        fragments.after("n.age > $_q0", {"_q0": 1})
    with pytest.raises(DrivineError, match="begins with _cy_"):
        fragments.bound_beside({"_cy_mine": 1})
    assert fragments.bound_beside({"mine": 1}) == {"mine": 1, "_cy__q0": '"Ada"'}
    assert fragments.where(None, {}) is None


def test_hints_are_kept_once_each_in_the_order_asked_and_written_after_the_outermost_keyword():
    fragments = Fragments("ORACLE", "g", "people_", ["Person"], INDEXED)
    fragments.where("n.name = $_q0", {"_q0": "Ada"})
    fragments.where("n.age > $_q1", {"_q1": 1})
    assert fragments.hints == ["NO_JSON_TABLE_TRANSFORM"]
    assert fragments.hinted("SELECT x FROM t WHERE (SELECT 1 FROM d) = 1") == (
        "SELECT /*+ NO_JSON_TABLE_TRANSFORM */ x FROM t WHERE (SELECT 1 FROM d) = 1"
    )
    assert fragments.hinted("  delete FROM t") == "  delete /*+ NO_JSON_TABLE_TRANSFORM */ FROM t"
    with pytest.raises(DrivineError, match="No SELECT or DELETE begins"):
        fragments.hinted("WITH a AS (SELECT 1) SELECT 2")
    unhinted = Fragments("POSTGRES", "g", "people_")
    unhinted.where("n.name = $_q0", {"_q0": "Ada"})
    assert unhinted.hinted("SELECT 1") == "SELECT 1"
    assert unhinted.hint() == ""


def test_a_value_that_may_be_nan_says_so():
    fragments = Fragments("POSTGRES", "g", "people_")
    fragments.value("n.a / 2.0")
    assert not fragments.non_finite
    fragments.value("n.a / 0.0")
    assert fragments.non_finite


STAMP = {
    "POSTGRES": ["to_jsonb(people_.stamp)"],
    "ORACLE": ["JSON_SCALAR(people_.stamp)"],
    "SQLITE": ["NULLIF(json_quote(people_.stamp), 'null')"],
}
"""
What the stamp, `n.`__drivine.stamp``, gives as a value on each store: the node row's own column, which the module
reads the stamp from, not a property of `props`, as the store layout keeps it (see drivine.model.stamps).
"""


@pytest.mark.parametrize("store", STORES)
def test_the_stamp_is_read_from_the_nodes_stamp_column(store: SharedStore):
    stamp = property_of("__drivine.stamp")
    assert stamp == "n.`__drivine.stamp`"
    assert list(compile_fragment(store, "g", "value", stamp, PEOPLE).terms) == STAMP[store]
    condition = compile_fragment(store, "g", "condition", f"{stamp} = $s", PEOPLE, {"s": "a:b"}).term
    assert "people_.stamp" in condition
    assert "__drivine" not in condition


def test_the_root_row_ends_with_an_underscore_and_oracle_joins_a_node_back_by_both_keys():
    assert FRAGMENT_ROOT_ROW == "n_"
    assert same_key("a.nid", "b.nid") == "(a.nid = b.nid + 0 AND b.nid = a.nid + 0)"
    with pytest.raises(DrivineError, match="A property name is empty"):
        property_of("")
    assert property_of("a`b") == "n.`a``b`"


def test_a_parameters_name_is_not_renamed_in_a_quoted_name_or_text():
    # A property may be named `$p` (a key that begins with `$` is read as it is held), and a text may hold `$p`.
    names = Names(1)
    assert names.named("n.`$p` = $p AND n.b = '$p' AND n.c = \"$p\"", ["p"]) == (
        "n.`$p` = $f1_p AND n.b = '$p' AND n.c = \"$p\""
    )
    assert names.named("n.`a``$p` = 'it''s $p\\' $p' + $p", ["p"]) == "n.`a``$p` = 'it''s $p\\' $p' + $f1_p"
    assert names.named("n.a = $p", []) == "n.a = $p"


def test_a_fragment_over_a_relationship_gives_its_row_and_no_labels():
    knows = FragmentVariable("knows_", relationship=True)
    assert knows.written() == {"relationship": "knows_"}
    term = compile_fragment("SQLITE", "g", "condition", "r.since > $s", {"r": knows}, {"s": 1}).term
    assert "knows_.props" in term


def test_only_an_order_asks_to_be_descending():
    # The module refuses a descending condition or value: a caller's descending is written for an order alone.
    for part in ("condition", "value"):
        compile_fragment("SQLITE", "g", part, "n.age > 1", PEOPLE, descending=True)
    ascending = compile_fragment("SQLITE", "g", "order", "n.age", PEOPLE).terms
    assert compile_fragment("SQLITE", "g", "order", "n.age", PEOPLE, descending=True).terms != ascending


def test_a_condition_on_another_row_is_not_given_the_roots_labels():
    fragments = Fragments("ORACLE", "g", "people_", ["Person"], INDEXED)
    assert "JSON_EXISTS(people_.labels" in fragments.condition("n.name = $p", {"p": "Ada"})
    other = fragments.condition("n.name = $p", {"p": "Ada"}, row="others_")
    assert "others_.props" in other
    assert "labels" not in other


def test_a_fragment_that_binds_a_name_bound_already_binds_none_of_its_own():
    fragments = Fragments("SQLITE", "g", "people_")
    fragments.where("n.name = $_q0", {"_q0": "Ada"})
    with pytest.raises(DrivineError, match="bound already"):
        fragments.where("n.age > $_q1 AND n.name = $_q0", {"_q0": "Ada", "_q1": 1})
    assert set(fragments.parameters) == {"_cy__q0"}


def test_a_callers_name_the_module_binds_on_oracle_is_refused_as_bound_already():
    # On Oracle the module binds a key compared by its index as `_cyk_<name>`, which isn't under `_cy_`.
    fragments = Fragments("ORACLE", "g", "people_", ["Person"], INDEXED)
    fragments.condition("n.name = $p", {"p": "Ada"})
    assert "_cyk_f1_p" in fragments.parameters
    with pytest.raises(DrivineError, match="The parameter _cyk_f1_p is bound already."):
        fragments.bound_beside({"_cyk_f1_p": "Ada"})


def test_oracle_says_which_of_the_graphs_functions_a_fragment_calls_and_calls_none_the_graph_lacks():
    year = compile_fragment("ORACLE", "g", "value", "n.born.year", PEOPLE)
    assert (year.calls_stored_era, year.calls_stored_nulls_equal) == (True, False)
    assert compile_fragment("ORACLE", "g", "value", "n.a / 0.0", PEOPLE).calls_stored_temporal_arithmetic
    # Where the graph was made without one, the module calls it not, as a statement's Fragments tells it.
    assert "g_era(" in Fragments("ORACLE", "g", "people_").value("n.born.year")
    assert "g_era(" not in Fragments("ORACLE", "g", "people_", era_stored=False).value("n.born.year")


def test_an_order_of_several_terms_is_no_condition_or_value():
    order = SqlFragment(("a", "b"), {})
    with pytest.raises(DrivineError, match="A fragment of 2 terms is no condition or value"):
        _ = order.term


def test_a_fragment_may_call_the_root_by_another_alias():
    # The DSL's where, a cursor's keyset, an order and a value may each name the root as the query does, not `n`.
    fragments = Fragments("SQLITE", "g", "people_", ["Person"])
    assert "people_.props" in (fragments.where("m.name = $_q0", {"_q0": "Ada"}, alias="m") or "")
    assert "people_.props" in fragments.after("m.age > $_q1", {"_q1": 1}, alias="m")
    assert "people_.props" in fragments.order("m.age", alias="m")[0]
    assert "people_.props" in fragments.value("m.age", alias="m")
