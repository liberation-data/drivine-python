"""drivine4j's OraclePredicatesTest for matches, ported: TypeScript's Drivine tests the same."""

from datetime import time, timedelta, timezone
from typing import Annotated, Any

import pytest

from drivine import DrivineError, ModelRegistry, NodeFragment, NodeId, refs
from drivine.backend.oracle.oracle_backend import OracleBackend
from drivine.backend.oracle.oracle_graph import OracleGraph
from drivine.backend.oracle.oracle_query_renderer import OracleQueryRenderer
from tests.models import Person


def matching(pattern: str) -> tuple[str, dict[str, Any]]:
    registry = ModelRegistry()
    return OracleBackend(OracleGraph()).render_where(
        refs(Person).name.matches(pattern), registry.fragment(Person), lambda type_: registry.fragment(type_).labels
    )


def bound(pattern: str) -> str:
    return next(iter(matching(pattern)[1].values()))


def test_matches_holds_an_expression_to_the_whole_value():
    where, bindings = matching("a.*")
    assert where == "REGEXP_LIKE(JSON_VALUE(n.props, '$.\"name\"'), $_q0)"
    assert bindings == {"_q0": "^(a.*)\\z"}


def test_matches_renumbers_a_backreference_for_the_group_that_anchors_the_expression():
    assert bound(r"(A).*\1") == r"^((A).*\2)\z"
    assert bound(r"(a)(b)\2\1") == r"^((a)(b)\3\2)\z"
    # An escaped backslash, and a backslash in a bracket expression, are no backreferences.
    assert bound(r"a\\1") == r"^(a\\1)\z"
    assert bound(r"[\1]x") == r"^([\1]x)\z"
    assert bound(r"[]\1](a)\1") == r"^([]\1](a)\2)\z"
    assert bound(r"[^]\1](a)\1") == r"^([^]\1](a)\2)\z"
    # An escaped bracket opens no bracket expression.
    assert bound(r"\[(a)\1]") == r"^(\[(a)\2])\z"


def test_matches_refuses_a_reference_to_a_ninth_group_which_would_need_a_tenth():
    with pytest.raises(DrivineError, match="ninth"):
        matching(r"(a)(b)(c)(d)(e)(f)(g)(h)(i)\9")


def test_matches_ignores_case_only_for_a_leading_case_flag_which_is_taken_off_the_expression():
    where, bindings = matching("(?i)a.*")
    assert where == "REGEXP_LIKE(JSON_VALUE(n.props, '$.\"name\"'), $_q0, 'i')"
    assert bindings == {"_q0": "^(a.*)\\z"}
    assert "'i'" not in matching("a(?i)b")[0]
    assert bound(r"(?i)(a)\1") == r"^((a)\2)\z"


class Clocked(NodeFragment, labels=["Clocked"]):
    id: Annotated[str, NodeId()]
    clock: time | None = None
    clocks: list[time] | None = None


@pytest.mark.parametrize("test", ["eq", "in", "has_item"])
def test_a_time_at_an_offset_with_seconds_is_refused_by_name_as_the_query_dsl_refuses_it_on_the_other_sql_stores(
    test: str, monkeypatch: pytest.MonkeyPatch
):
    """
    Oracle's renderer writes its value through _storable too (#523), so it can't bind one no stored value matches. It
    refuses any comparison with a time first, by the name of drivine-cypher-spec#235: with that refusal lifted, as it
    will be when the DSL takes the shared module's fragments, the offset is refused.
    """
    at = time(10, tzinfo=timezone(timedelta(hours=1, seconds=1)))
    clocked = refs(Clocked)
    tested = {
        "eq": lambda: clocked.clock.eq(at),
        "in": lambda: clocked.clock.in_([time(9), at]),
        "has_item": lambda: clocked.clocks.has_item(at),
    }[test]()
    registry = ModelRegistry()

    def render() -> None:
        OracleBackend(OracleGraph()).render_where(
            tested, registry.fragment(Clocked), lambda type_: registry.fragment(type_).labels
        )

    with pytest.raises(DrivineError, match=r"aren't supported on Oracle yet .*drivine-cypher-spec#235"):
        render()
    monkeypatch.setattr(OracleQueryRenderer, "reads_temporals", True)
    with pytest.raises(DrivineError, match="at an offset with seconds"):
        render()
